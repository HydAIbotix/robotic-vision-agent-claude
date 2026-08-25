"""
Parse the kiosk codebase + product artifacts into a local Chroma vector index.

This is the POC's ParseCodeAndStore.py brought into the backend almost verbatim. The
retrieval brain — Tree-sitter code chunking, HuggingFace sentence-transformer embeddings,
and the Chroma vector store — is LEFT AS-IS. The only refinements are:

  • Paths are now config-driven (vision_agent.config.settings) instead of hardcoded, so the
    same code runs on any machine. CODEBASE_DIR points at the LIVE kiosk app under test and
    the design-doc / test-case artifacts live under ./docs (they were moved there).
  • Tree-sitter is imported lazily and degrades to plain text chunking if the grammar wheels
    aren't installed, so indexing never hard-fails on a fresh environment.

Build the index:   python -m repair_agent.parse_code_and_store
Query it:          query_codebase("valid login credentials not working")
"""
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

from langchain_core.documents import Document
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

from vision_agent.config import settings

# Tree-sitter is optional: if the grammar wheels aren't present we still index code as text.
try:
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_typescript as tstypescript
    _TREE_SITTER_OK = True
except Exception as _e:  # pragma: no cover - depends on the host env
    print(f"[RAG] tree-sitter unavailable ({_e}); code will be indexed as plain text.")
    _TREE_SITTER_OK = False


# --- Configuration (resolved from settings, relative to the repo root) ---
REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


CODEBASE_DIR = _resolve(settings.repair_codebase_dir)
DOCS_DIR = _resolve(settings.repair_docs_dir)
DESIGN_DOC_PATH = DOCS_DIR / "Kiosk_POS_and_SmartCardStation_Production_Design.docx"
TEST_CASES_PATH = DOCS_DIR / "kiosk_e2e_tests.xlsx"
PERSIST_DIR = _resolve(settings.repair_persist_dir)
EMBEDDING_MODEL_NAME = settings.repair_embedding_model

CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}
TEXT_EXTENSIONS = {".css", ".json", ".md"}
# Skip build output, deps, and stray non-source folders that would pollute retrieval. Any
# dot-directory (.git, .vite, .rag, …) is skipped too — a prior POC left a `.rag/` index dump
# and a repair_brief.md inside the live app that otherwise dominated similarity search and fed
# the LLM stale, hallucinated markup. We index only real source + product artifacts.
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".vite", "__pycache__", "chroma_code_db",
    "artifacts", "assets", "docs", "docx_render_check", "exports", "tmp", "coverage",
}
# Lock files are huge and pure dependency noise — they crowd out real code in similarity search.
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "package-lock.jsonc"}


def get_typescript_language(file_path):
    """Returns the right Tree-sitter grammar for TypeScript or TSX/JSX files."""
    suffix = Path(file_path).suffix.lower()
    if suffix in {".tsx", ".jsx"}:
        return Language(tstypescript.language_tsx())
    return Language(tstypescript.language_typescript())


def make_document(file_path, doc_type, content, start_line=None, end_line=None, extra_metadata=None):
    metadata = {
        "source": str(file_path),
        "type": doc_type,
    }
    if start_line is not None:
        metadata["start_line"] = start_line
    if end_line is not None:
        metadata["end_line"] = end_line
    if extra_metadata:
        metadata.update(extra_metadata)
    return Document(page_content=content.strip(), metadata=metadata)


def chunk_text(text, file_path, doc_type, chunk_size=2200, overlap=300, extra_metadata=None):
    """Chunks non-code artifacts so design docs, tests, and supporting files can be searched."""
    documents = []
    text = text.strip()
    if not text:
        return documents

    start = 0
    chunk_number = 1
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        metadata = {"chunk": chunk_number, "start_char": start, "end_char": end}
        if extra_metadata:
            metadata.update(extra_metadata)
        documents.append(make_document(file_path, doc_type, chunk, extra_metadata=metadata))
        if end == len(text):
            break
        start = max(0, end - overlap)
        chunk_number += 1

    return documents


def parse_code_with_tree_sitter(file_path):
    """Parses TypeScript/TSX/JavaScript files and extracts logical code blocks."""
    if not _TREE_SITTER_OK:
        with open(file_path, "rb") as f:
            text = f.read().decode("utf8", errors="ignore")
        return chunk_text(text, file_path, "code_file", extra_metadata={
            "language": Path(file_path).suffix.lower().lstrip("."),
            "parser": "text-fallback",
        })

    language = get_typescript_language(file_path)
    parser = Parser(language)

    with open(file_path, "rb") as f:
        code_bytes = f.read()

    tree = parser.parse(code_bytes)

    query_scm = """
    (function_declaration name: (identifier) @name) @definition
    (class_declaration name: (type_identifier) @name) @definition
    (method_definition name: (property_identifier) @name) @definition
    (lexical_declaration
      (variable_declarator
        name: (identifier) @name
        value: [(arrow_function) (function_expression)]) @definition)
    """

    query = Query(language, query_scm)
    captures = QueryCursor(query).captures(tree.root_node)
    definition_nodes = captures.get("definition", [])

    documents = []
    seen_ranges = set()
    for node in definition_nodes:
        node_range = (node.start_byte, node.end_byte)
        if node_range in seen_ranges:
            continue
        seen_ranges.add(node_range)

        snippet = code_bytes[node.start_byte:node.end_byte].decode("utf8", errors="ignore")
        documents.append(make_document(
            file_path=file_path,
            doc_type="code_block",
            content=snippet,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            extra_metadata={"language": Path(file_path).suffix.lower().lstrip(".")}
        ))

    # Some React files contain top-level JSX or object configuration that may not be
    # captured as functions/classes. Keep the whole file searchable as a fallback.
    if not documents:
        text = code_bytes.decode("utf8", errors="ignore")
        documents.extend(chunk_text(text, file_path, "code_file", extra_metadata={
            "language": Path(file_path).suffix.lower().lstrip("."),
            "parser": "tree-sitter-typescript-fallback"
        }))

    return documents


def parse_text_file(file_path):
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return chunk_text(f.read(), file_path, "supporting_file")


def extract_docx_text(file_path):
    if not Path(file_path).exists():
        return []

    with zipfile.ZipFile(file_path) as docx:
        xml_content = docx.read("word/document.xml")

    root = ET.fromstring(xml_content)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        if text:
            paragraphs.append(text)

    return chunk_text("\n".join(paragraphs), file_path, "design_document")


def extract_xlsx_text(file_path):
    if not Path(file_path).exists():
        return []

    documents = []
    with zipfile.ZipFile(file_path) as workbook:
        shared_strings = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in shared_root.findall("main:si", namespace):
                shared_strings.append("".join(t.text or "" for t in item.findall(".//main:t", namespace)))

        sheet_files = sorted(name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for sheet_number, sheet_file in enumerate(sheet_files, start=1):
            root = ET.fromstring(workbook.read(sheet_file))
            rows = []
            for row in root.findall(".//main:row", namespace):
                cells = []
                for cell in row.findall("main:c", namespace):
                    value_node = cell.find("main:v", namespace)
                    if value_node is None or value_node.text is None:
                        continue
                    value = value_node.text
                    if cell.attrib.get("t") == "s":
                        value = shared_strings[int(value)] if value.isdigit() and int(value) < len(shared_strings) else value
                    cells.append(unescape(value))
                if cells:
                    rows.append(" | ".join(cells))

            documents.extend(chunk_text("\n".join(rows), file_path, "test_cases", extra_metadata={"sheet": sheet_number}))

    return documents


def build_codebase_index():
    """Walks through artifacts, parses files, and populates ChromaDB."""
    all_documents = []

    print(f"Scanning codebase directory: {CODEBASE_DIR}...")
    if not CODEBASE_DIR.exists():
        print(f"Codebase directory does not exist: {CODEBASE_DIR}")
        return

    for dirpath, dirnames, filenames in os.walk(CODEBASE_DIR):
        # Drop skip-listed folders and ALL dot-directories (.rag, .git, .vite, …).
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]

        for file in filenames:
            if file in SKIP_FILES:
                continue
            file_path = Path(dirpath) / file
            suffix = file_path.suffix.lower()
            try:
                if suffix in CODE_EXTENSIONS:
                    all_documents.extend(parse_code_with_tree_sitter(file_path))
                elif suffix in TEXT_EXTENSIONS:
                    all_documents.extend(parse_text_file(file_path))
            except Exception as e:
                print(f"Error parsing {file_path}: {e}")

    all_documents.extend(extract_docx_text(DESIGN_DOC_PATH))
    all_documents.extend(extract_xlsx_text(TEST_CASES_PATH))

    if not all_documents:
        print("No valid chunks found to index.")
        return

    print(f"Extracted {len(all_documents)} searchable chunks.")

    print("Generating embeddings and saving to ChromaDB...")
    embedding_model = _get_embedding_model()   # cached, shared with search (same model name)

    # Reset the collection IN-PLACE rather than deleting the folder. On Windows the running backend
    # keeps the Chroma sqlite open (a prior repair query), so `shutil.rmtree(PERSIST_DIR)` fails with
    # WinError 32 and can leave a half-deleted, corrupt index. Reusing the same persistent client to
    # drop + recreate the collection sidesteps the file lock entirely, so "Rebuild index" works while
    # the backend is running — no restart required.
    try:
        db = Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embedding_model)
        try:
            db.delete_collection()
        except Exception as e:
            print(f"  (could not drop old collection, continuing: {e})")
        db = Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embedding_model)
        db.add_documents(all_documents)
    except Exception as e:
        # A previous rmtree that half-deleted the dir can leave it un-openable. As a fresh process
        # holds no lock, fall back to a clean rebuild from scratch.
        print(f"  In-place reset failed ({e}); rebuilding the index directory from scratch.")
        import shutil
        if PERSIST_DIR.exists():
            shutil.rmtree(PERSIST_DIR, ignore_errors=True)
        Chroma.from_documents(
            documents=all_documents, embedding=embedding_model,
            persist_directory=str(PERSIST_DIR),
        )
    print(f"Indexing complete! {len(all_documents)} chunks persisted at {PERSIST_DIR}.")
    return len(all_documents)


_EMBEDDING_MODEL = None


def _get_embedding_model():
    """Load the HuggingFace embedding model once per process and reuse it.

    Loading all-MiniLM-L6-v2 pulls ~90 MB of weights + torch init — SECONDS each time, and it is the
    real cost of a repair retrieve (the vector lookup over a few hundred chunks is milliseconds). The
    old code re-instantiated the model on EVERY `search()` call, and one `retrieve_context` fires
    several searches (code + general + per-file expansion), so a single retrieve reloaded the model
    4-8×. Caching it at module scope makes retrieve fast once the index is built. The model is stateless
    w.r.t. the index, so this is safe across an in-place "Rebuild index" (see `_vector_db`)."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        _EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL_NAME)
    return _EMBEDDING_MODEL


def _vector_db():
    # Reuse the cached embedding model (expensive to load); always open a FRESH Chroma handle so every
    # search reflects the CURRENT on-disk collection. This keeps the speedup while staying immune to an
    # in-place "Rebuild index" — whether triggered in this backend process or a separate one — with no
    # stale-handle risk. Opening the handle just reads the small sqlite; the model load was the bottleneck.
    return Chroma(persist_directory=str(PERSIST_DIR), embedding_function=_get_embedding_model())


def search(query_text, k=4, where=None):
    """Semantic search over the Chroma index, with an optional metadata filter (e.g.
    {"type": {"$in": ["code_block", "code_file"]}} to bias toward source code)."""
    vector_db = _vector_db()
    if where:
        return vector_db.similarity_search(query_text, k=k, filter=where)
    return vector_db.similarity_search(query_text, k=k)


def query_codebase(query_text, k=4, quiet=False):
    """Queries the local Chroma vector database semantically. Returns the raw docs."""
    if not quiet:
        print(f"\nSearching artifacts for: '{query_text}'\n" + "-" * 50)

    results = search(query_text, k=k)

    if not quiet:
        for i, doc in enumerate(results):
            print(f"Result {i + 1}:")
            print(f"  File: {doc.metadata.get('source')}")
            print(f"  Type: {doc.metadata.get('type')}")
            if doc.metadata.get("start_line"):
                print(f"  Lines: {doc.metadata.get('start_line')} - {doc.metadata.get('end_line')}")
            if doc.metadata.get("sheet"):
                print(f"  Sheet: {doc.metadata.get('sheet')}")
            snippet = re.sub(r"\n{3,}", "\n\n", doc.page_content.strip())
            print(f"  Snippet:\n{snippet[:1200]}")
            print("-" * 50)

    return results


if __name__ == "__main__":
    build_codebase_index()

    sample_query = "valid login credentials not working. Fix the issue."
    query_codebase(sample_query)

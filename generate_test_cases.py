#!/usr/bin/env python3
"""
Generate 50 end-to-end test cases covering Kiosk-1 (Smart Card Station)
and Kiosk-2 (POS Kiosk) and write them to an Excel file.

Usage:
    python generate_test_cases.py
"""
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ── 50 test cases ──────────────────────────────────────────────────────────────

CASES = [
    # ── Kiosk-1: Smart Card Station (10 tests) ─────────────────────────────────
    {
        "Local Test ID":  "TC-K1-001",
        "Summary":        "[P1] [smoke] Load $100 mock card on Kiosk-1",
        "Description":    "Verify that the smart card station issues a mock card with $100 balance.",
        "Preconditions":  "Kiosk-1 is running at KIOSK-ID-1. No card loaded.",
        "Test Steps":     "1. Navigate to Kiosk-1 card station URL (?kiosk=card-station)\n2. Set load amount to 100\n3. Tap 'Use Mock Card' button",
        "Expected Results":"Card station shows 'Smart Card Loaded' with card number and balance $100.00. Card appears in the Active Cards ledger.",
    },
    {
        "Local Test ID":  "TC-K1-002",
        "Summary":        "[P1] [smoke] Load $500 mock card on Kiosk-1",
        "Description":    "Verify that a mock card with $500 can be loaded.",
        "Preconditions":  "Kiosk-1 is running.",
        "Test Steps":     "1. Navigate to Kiosk-1 card station\n2. Set amount to 500\n3. Tap 'Use Mock Card'",
        "Expected Results":"Card loaded with balance $500.00. Card visible in ledger.",
    },
    {
        "Local Test ID":  "TC-K1-003",
        "Summary":        "[P2] [regression] Verify load amount field accepts numeric input only",
        "Description":    "Ensure the amount field rejects non-numeric input.",
        "Preconditions":  "Kiosk-1 is running.",
        "Test Steps":     "1. Navigate to Kiosk-1 card station\n2. Clear load amount field\n3. Type 'abc'\n4. Observe field value",
        "Expected Results":"Field does not accept alphabetic characters. Value stays at previous or 0.",
    },
    {
        "Local Test ID":  "TC-K1-004",
        "Summary":        "[P2] [regression] Active cards ledger shows all issued cards",
        "Description":    "After loading two cards, ledger shows both.",
        "Preconditions":  "No cards loaded.",
        "Test Steps":     "1. Load mock card #1 ($100)\n2. Load mock card #2 ($200)\n3. Check ledger section",
        "Expected Results":"Ledger displays both cards with correct balances and statuses.",
    },
    {
        "Local Test ID":  "TC-K1-005",
        "Summary":        "[P3] [regression] Default load amount is 500",
        "Description":    "Verify the default pre-filled amount is 500.",
        "Preconditions":  "Kiosk-1 fresh reload.",
        "Test Steps":     "1. Navigate to Kiosk-1 card station\n2. Read the load amount field value without changing it",
        "Expected Results":"Load amount field shows 500.",
    },
    {
        "Local Test ID":  "TC-K1-006",
        "Summary":        "[P2] [smoke] Arm reader for real card tap",
        "Description":    "Tapping 'Tap Real Card' arms the reader and shows the capture area.",
        "Preconditions":  "Kiosk-1 is running.",
        "Test Steps":     "1. Navigate to Kiosk-1\n2. Tap 'Tap Real Card' button",
        "Expected Results":"Reader capture box appears with 'Reader armed. Present smart card once.'",
    },
    {
        "Local Test ID":  "TC-K1-007",
        "Summary":        "[P3] [regression] Load amount minimum is 1",
        "Description":    "Verify amount 0 or negative is rejected.",
        "Preconditions":  "Kiosk-1 running.",
        "Test Steps":     "1. Set load amount to 0\n2. Tap 'Use Mock Card'",
        "Expected Results":"No card is issued (amount <= 0 is rejected by issueCard).",
    },
    {
        "Local Test ID":  "TC-K1-008",
        "Summary":        "[P2] [regression] Card station shows KIOSK-ID-1 identifier",
        "Description":    "The kiosk ID badge shows the correct station identifier.",
        "Preconditions":  "Kiosk-1 running.",
        "Test Steps":     "1. Navigate to Kiosk-1\n2. Read the kiosk ID badge in the header",
        "Expected Results":"Kiosk ID badge shows 'KIOSK-ID-1'.",
    },
    {
        "Local Test ID":  "TC-K1-009",
        "Summary":        "[P1] [smoke] Loaded card balance persists across page reload",
        "Description":    "Verify that issued card balance is stored in localStorage and survives reload.",
        "Preconditions":  "No card loaded.",
        "Test Steps":     "1. Load mock card ($300)\n2. Reload the page\n3. Check ledger",
        "Expected Results":"Card with $300 balance still appears in ledger after reload.",
    },
    {
        "Local Test ID":  "TC-K1-010",
        "Summary":        "[P1] [regression] Card issued at Kiosk-1 is visible at Kiosk-2 card reader",
        "Description":    "Verify cross-kiosk card visibility — card loaded at K1 can be used at K2.",
        "Preconditions":  "Both kiosks running (same browser profile / localStorage).",
        "Test Steps":     "1. Load mock card ($250) at Kiosk-1\n2. Note card number\n3. Navigate to Kiosk-2\n4. Sign in\n5. Add product to cart\n6. Proceed to payment\n7. Enter card number at tap screen",
        "Expected Results":"Kiosk-2 recognises the ValuePass smart card with $250 balance.",
    },

    # ── Kiosk-2: POS Kiosk — Auth (10 tests) ──────────────────────────────────
    {
        "Local Test ID":  "TC-K2-001",
        "Summary":        "[P1] [smoke] valid sign-in opens products page",
        "Description":    "Sign in with valid credentials and verify products page loads.",
        "Preconditions":  "User tester@kiosk.local exists (Password123).",
        "Test Steps":     "1. Navigate to Kiosk-2 sign-in\n2. Enter email: tester@kiosk.local\n3. Enter password: Password123\n4. Tap 'Sign In'",
        "Expected Results":"Products page is displayed. Login success status shown.",
    },
    {
        "Local Test ID":  "TC-K2-002",
        "Summary":        "[P1] [regression] invalid credentials shows login-failed popup",
        "Description":    "Wrong password triggers popup and stays on sign-in.",
        "Preconditions":  "Kiosk-2 on sign-in screen.",
        "Test Steps":     "1. Enter email: tester@kiosk.local\n2. Enter password: WrongPassword\n3. Tap 'Sign In'",
        "Expected Results":"'Login Failed' popup appears. User stays on sign-in screen.",
    },
    {
        "Local Test ID":  "TC-K2-003",
        "Summary":        "[P2] [regression] sign-up creates new user and redirects to products",
        "Description":    "New user can register and is immediately logged in.",
        "Preconditions":  "No existing user with email newuser@test.com.",
        "Test Steps":     "1. Tap 'Sign up'\n2. Enter name: Robot Tester\n3. Enter email: newuser@test.com\n4. Enter password: TestPass1\n5. Tap 'Sign Up and Continue'",
        "Expected Results":"User is created and redirected to products page.",
    },
    {
        "Local Test ID":  "TC-K2-004",
        "Summary":        "[P2] [regression] forgot password screen accepts email and returns to sign-in",
        "Description":    "Password reset flow navigates correctly.",
        "Preconditions":  "On sign-in screen.",
        "Test Steps":     "1. Tap 'Forgot password?'\n2. Enter email: tester@kiosk.local\n3. Tap 'Send Mock Reset'",
        "Expected Results":"Returns to sign-in screen with password reset status message.",
    },
    {
        "Local Test ID":  "TC-K2-005",
        "Summary":        "[P2] [regression] sign-out returns to sign-in screen",
        "Description":    "Signing out clears session and shows sign-in.",
        "Preconditions":  "Logged in as tester@kiosk.local.",
        "Test Steps":     "1. Tap 'Sign Out' button in header",
        "Expected Results":"Sign-in screen is displayed. Cart is empty.",
    },

    # ── Kiosk-2: POS — Products & Cart (10 tests) ─────────────────────────────
    {
        "Local Test ID":  "TC-K2-006",
        "Summary":        "[P1] [smoke] add product to cart and verify cart count",
        "Description":    "Adding a product increments the cart counter.",
        "Preconditions":  "Logged in. Cart is empty.",
        "Test Steps":     "1. Set quantity to 1 on first product\n2. Tap 'Add to Cart'",
        "Expected Results":"Product-added popup appears. Cart count shows 1.",
    },
    {
        "Local Test ID":  "TC-K2-007",
        "Summary":        "[P1] [regression] cart shows correct subtotal and tax",
        "Description":    "Cart totals are correctly calculated with 8.25% tax.",
        "Preconditions":  "One product ($10.00) in cart.",
        "Test Steps":     "1. Navigate to Cart\n2. Check subtotal, tax, and total values",
        "Expected Results":"Subtotal=$10.00, Tax=$0.83, Total=$10.83.",
    },
    {
        "Local Test ID":  "TC-K2-008",
        "Summary":        "[P2] [regression] increase cart quantity via cart page controls",
        "Description":    "Cart quantity controls update item count and total.",
        "Preconditions":  "One item in cart (qty 1).",
        "Test Steps":     "1. Go to Cart\n2. Tap + button next to item\n3. Check quantity shows 2",
        "Expected Results":"Item quantity becomes 2. Total price doubles.",
    },
    {
        "Local Test ID":  "TC-K2-009",
        "Summary":        "[P2] [regression] remove item from cart by setting qty to 0",
        "Description":    "Reducing quantity to 0 removes the item.",
        "Preconditions":  "One item in cart.",
        "Test Steps":     "1. Go to Cart\n2. Tap – button until quantity shows 0",
        "Expected Results":"Item is removed from cart. Cart shows empty state.",
    },
    {
        "Local Test ID":  "TC-K2-010",
        "Summary":        "[P2] [regression] adding zero-quantity shows popup",
        "Description":    "Tapping 'Add to Cart' without selecting quantity shows error popup.",
        "Preconditions":  "On Products page.",
        "Test Steps":     "1. Leave quantity at 0 for a product\n2. Tap 'Add to Cart'",
        "Expected Results":"'Quantity Required' popup is shown.",
    },

    # ── Kiosk-2: POS — Payment (10 tests) ─────────────────────────────────────
    {
        "Local Test ID":  "TC-K2-011",
        "Summary":        "[P1] [smoke] complete purchase with mock card approval",
        "Description":    "Full purchase flow with mock card reader.",
        "Preconditions":  "Logged in. One product in cart.",
        "Test Steps":     "1. Go to Cart\n2. Tap 'Proceed to Card Payment'\n3. Tap 'Start Card Reader Session'\n4. Tap 'Use Mock Card Approval'\n5. Observe result screen",
        "Expected Results":"Order result screen shows SUCCESS with order ID.",
    },
    {
        "Local Test ID":  "TC-K2-012",
        "Summary":        "[P1] [smoke] order history shows completed order after purchase",
        "Description":    "Completed order appears in order history.",
        "Preconditions":  "One order completed.",
        "Test Steps":     "1. After successful purchase, tap 'Order History'\n2. Verify order is listed",
        "Expected Results":"Order appears in history with correct items and total.",
    },
    {
        "Local Test ID":  "TC-K2-013",
        "Summary":        "[P1] [smoke] pay with ValuePass smart card (cross-kiosk)",
        "Description":    "Use a smart card loaded at Kiosk-1 to pay at Kiosk-2.",
        "Preconditions":  "Smart card with $500 balance loaded at Kiosk-1 (same localStorage).",
        "Test Steps":     "1. Log in to Kiosk-2\n2. Add $50 product to cart\n3. Proceed to card payment\n4. On tap-card screen, enter smart card number\n5. Confirm payment",
        "Expected Results":"Payment approved. Balance deducted from smart card. Order result shows SUCCESS.",
    },
    {
        "Local Test ID":  "TC-K2-014",
        "Summary":        "[P1] [regression] insufficient smart card balance shows declined",
        "Description":    "Payment is declined when card balance is less than order total.",
        "Preconditions":  "Smart card with $10 balance. Product costs $50.",
        "Test Steps":     "1. Load $10 smart card at Kiosk-1\n2. At Kiosk-2 add $50 product\n3. Try paying with the $10 card",
        "Expected Results":"Payment declined with 'Insufficient balance' message.",
    },
    {
        "Local Test ID":  "TC-K2-015",
        "Summary":        "[P2] [regression] high-value order (>$1000) shows confirmation popup",
        "Description":    "Order above $1000 triggers a confirmation popup.",
        "Preconditions":  "Popups enabled. Cart total > $1000.",
        "Test Steps":     "1. Add quantity 10 of expensive product to reach >$1000\n2. Proceed to checkout",
        "Expected Results":"'High Value Order Confirmation' popup is shown before going to tap screen.",
    },
    {
        "Local Test ID":  "TC-K2-016",
        "Summary":        "[P2] [regression] back button from cart returns to products",
        "Description":    "Cart 'Continue Shopping' returns to products.",
        "Preconditions":  "On Cart screen.",
        "Test Steps":     "1. Tap 'Continue Shopping' button",
        "Expected Results":"Products page is displayed.",
    },
    {
        "Local Test ID":  "TC-K2-017",
        "Summary":        "[P2] [regression] payment screen back button returns to cart",
        "Description":    "Back button on payment screen returns to cart.",
        "Preconditions":  "On Payment screen.",
        "Test Steps":     "1. Tap 'Back to Cart' button",
        "Expected Results":"Cart screen is displayed with items still in cart.",
    },
    {
        "Local Test ID":  "TC-K2-018",
        "Summary":        "[P3] [regression] order result go-home button returns to products",
        "Description":    "After purchase, 'Go Home' navigates to products.",
        "Preconditions":  "On Order Result screen.",
        "Test Steps":     "1. Tap 'Go Home' or 'Continue Shopping'",
        "Expected Results":"Products page is displayed. Cart is empty.",
    },
    {
        "Local Test ID":  "TC-K2-019",
        "Summary":        "[P3] [regression] empty cart prevents checkout button",
        "Description":    "Cart/Checkout button is disabled when cart is empty.",
        "Preconditions":  "Cart is empty.",
        "Test Steps":     "1. On Products page with empty cart\n2. Check Cart/Checkout button state",
        "Expected Results":"Cart/Checkout button is disabled (grayed out).",
    },
    {
        "Local Test ID":  "TC-K2-020",
        "Summary":        "[P2] [regression] categories page lists all product categories",
        "Description":    "Categories nav item shows all distinct product categories.",
        "Preconditions":  "Logged in.",
        "Test Steps":     "1. Tap 'Categories' in sidebar\n2. Count category cards",
        "Expected Results":"All distinct product categories are displayed.",
    },

    # ── Kiosk-2: POS — Navigation & UI (5 tests) ──────────────────────────────
    {
        "Local Test ID":  "TC-K2-021",
        "Summary":        "[P3] [regression] deals page shows featured products",
        "Description":    "Deals page displays promotional products.",
        "Preconditions":  "Logged in.",
        "Test Steps":     "1. Tap 'Deals' in sidebar",
        "Expected Results":"Deals page shows at least 3 promoted products.",
    },
    {
        "Local Test ID":  "TC-K2-022",
        "Summary":        "[P3] [regression] loyalty page shows member tier and points",
        "Description":    "Loyalty screen displays user's tier and point balance.",
        "Preconditions":  "Logged in.",
        "Test Steps":     "1. Tap 'Loyalty' in sidebar",
        "Expected Results":"Loyalty page shows member email, tier, points, and reward values.",
    },
    {
        "Local Test ID":  "TC-K2-023",
        "Summary":        "[P3] [regression] receipts page shows latest order",
        "Description":    "Receipts page shows the most recent purchase receipt.",
        "Preconditions":  "At least one order completed.",
        "Test Steps":     "1. Tap 'Receipts' in sidebar",
        "Expected Results":"Latest receipt card with order details and email/print buttons is shown.",
    },
    {
        "Local Test ID":  "TC-K2-024",
        "Summary":        "[P3] [regression] support page shows three support cards",
        "Description":    "Support screen displays Contact, Reader Check, and Network Status.",
        "Preconditions":  "Logged in.",
        "Test Steps":     "1. Tap 'Support' in sidebar",
        "Expected Results":"Three support cards visible: Contact, Reader Check, Network Status.",
    },
    {
        "Local Test ID":  "TC-K2-025",
        "Summary":        "[P3] [regression] store info page shows kiosk ID and pair kiosk",
        "Description":    "Store info shows KIOSK-ID-2 and paired KIOSK-ID-1.",
        "Preconditions":  "Logged in.",
        "Test Steps":     "1. Tap 'Store Info' in sidebar",
        "Expected Results":"Kiosk ID=KIOSK-ID-2, Pair Kiosk=KIOSK-ID-1 displayed.",
    },

    # ── End-to-End: Cross-Kiosk (15 tests) ────────────────────────────────────
    {
        "Local Test ID":  "TC-E2E-001",
        "Summary":        "[P1] [e2e] [smoke] load card at K1 then buy product at K2",
        "Description":    "Full end-to-end: load $200 card at Kiosk-1, buy $50 product at Kiosk-2.",
        "Preconditions":  "Both kiosks running. User tester@kiosk.local exists.",
        "Test Steps":     "1. At Kiosk-1: load $200 mock card, note card number\n2. At Kiosk-2: sign in\n3. Add $50 product (qty 1) to cart\n4. Proceed to payment\n5. Enter card number at tap screen\n6. Complete payment",
        "Expected Results":"Order SUCCESS at Kiosk-2. Smart card balance reduced to $150.",
    },
    {
        "Local Test ID":  "TC-E2E-002",
        "Summary":        "[P1] [e2e] verify K1 card balance updated after K2 purchase",
        "Description":    "After buying at Kiosk-2, returning to Kiosk-1 shows reduced balance.",
        "Preconditions":  "TC-E2E-001 completed. Card has $150 remaining.",
        "Test Steps":     "1. Navigate to Kiosk-1 ledger\n2. Locate card number\n3. Read balance",
        "Expected Results":"Card balance shows $150.00 in the K1 ledger.",
    },
    {
        "Local Test ID":  "TC-E2E-003",
        "Summary":        "[P1] [e2e] load card twice and verify cumulative balance",
        "Description":    "Load same card twice at Kiosk-1 and verify total.",
        "Preconditions":  "No existing card.",
        "Test Steps":     "1. Load mock card $100 at Kiosk-1 — note card A\n2. Load another mock card $200 at Kiosk-1 — note card B\n3. Check ledger",
        "Expected Results":"Two separate cards visible in ledger with $100 and $200 balances.",
    },
    {
        "Local Test ID":  "TC-E2E-004",
        "Summary":        "[P1] [e2e] multiple products purchase via smart card",
        "Description":    "Buy 2 different products in one order using smart card.",
        "Preconditions":  "Smart card with $500. Kiosk-2 logged in.",
        "Test Steps":     "1. Add product A (qty 1) to cart\n2. Add product B (qty 2) to cart\n3. Proceed to payment\n4. Pay with smart card",
        "Expected Results":"Order SUCCESS with 2 line items. Card balance reduced by order total.",
    },
    {
        "Local Test ID":  "TC-E2E-005",
        "Summary":        "[P1] [e2e] declined card does not create order",
        "Description":    "A declined smart card payment leaves cart intact.",
        "Preconditions":  "Card with $5. Product costs $50.",
        "Test Steps":     "1. Add $50 product to cart\n2. Attempt to pay with $5 card",
        "Expected Results":"Payment declined. Cart is not cleared. No order in history.",
    },
    {
        "Local Test ID":  "TC-E2E-006",
        "Summary":        "[P2] [e2e] full session: sign-in, buy, sign-out, verify order in history",
        "Description":    "Complete session with order history check after sign-out.",
        "Preconditions":  "Fresh session.",
        "Test Steps":     "1. Sign in\n2. Add product\n3. Pay with mock card\n4. View order history\n5. Sign out\n6. Sign in again\n7. View order history",
        "Expected Results":"Order history persists across sign-out and re-login.",
    },
    {
        "Local Test ID":  "TC-E2E-007",
        "Summary":        "[P2] [e2e] robot picks card from tray and taps K1 reader",
        "Description":    "Physical robot card workflow: pick card, tap reader, verify K1 loads card.",
        "Preconditions":  "Real robot mode. Card in tray. Kiosk-1 armed for real card.",
        "Test Steps":     "1. At Kiosk-1 tap 'Tap Real Card'\n2. Robot: card_pick()\n3. Robot: card_tap('KIOSK-ID-1')\n4. Verify K1 shows loaded card\n5. Robot: card_replace()",
        "Expected Results":"Kiosk-1 detects card and shows loaded balance. Robot returns card to tray.",
    },
    {
        "Local Test ID":  "TC-E2E-008",
        "Summary":        "[P2] [e2e] robot navigates K1 then K2 sequentially",
        "Description":    "Robot drives from K1 to K2 between test steps.",
        "Preconditions":  "Real robot mode. Both kiosks configured with AprilTags.",
        "Test Steps":     "1. navigate_to_kiosk('K-01')\n2. Load card at Kiosk-1\n3. navigate_to_kiosk('K-02')\n4. Sign in, buy product, pay with card",
        "Expected Results":"Robot successfully drives to each kiosk. Both operations complete.",
    },
    {
        "Local Test ID":  "TC-E2E-009",
        "Summary":        "[P2] [e2e] order history shows multi-session orders for same user",
        "Description":    "Orders from multiple sessions are all visible in history.",
        "Preconditions":  "User with 2 prior orders.",
        "Test Steps":     "1. Sign in\n2. Go to Order History",
        "Expected Results":"All prior orders appear in order history list.",
    },
    {
        "Local Test ID":  "TC-E2E-010",
        "Summary":        "[P2] [e2e] receipts page shows latest purchase receipt",
        "Description":    "After purchase, receipts page shows the new order.",
        "Preconditions":  "One order just completed.",
        "Test Steps":     "1. After successful order, tap 'Receipts' in sidebar",
        "Expected Results":"Latest receipt shows correct items, amounts, and payment details.",
    },
    {
        "Local Test ID":  "TC-E2E-011",
        "Summary":        "[P3] [e2e] two robots run K1 and K2 tests concurrently",
        "Description":    "Parallel supervisor runs K1 tests on R-01 and K2 tests on R-02 simultaneously.",
        "Preconditions":  "run_parallel.py configured. Two robots available.",
        "Test Steps":     "1. python run_parallel.py with R-01→K-01 and R-02→K-02 assignments",
        "Expected Results":"Both suites complete. Results JSON shows results from both robots.",
    },
    {
        "Local Test ID":  "TC-E2E-012",
        "Summary":        "[P1] [e2e] smart card balance check after partial spend",
        "Description":    "Buy $30 product from $100 card; verify remaining $70.",
        "Preconditions":  "Card with $100 loaded at K1.",
        "Test Steps":     "1. At K2 buy $30 product with smart card\n2. Return to K1 ledger",
        "Expected Results":"Card balance shows $70.00.",
    },
    {
        "Local Test ID":  "TC-E2E-013",
        "Summary":        "[P1] [e2e] exact balance purchase completes successfully",
        "Description":    "Pay exact card balance — should succeed with $0 remaining.",
        "Preconditions":  "Card with exactly $50. Order total = $50.",
        "Test Steps":     "1. Load $50 card at K1\n2. Buy $50 product at K2",
        "Expected Results":"Payment approved. Card balance shows $0.00.",
    },
    {
        "Local Test ID":  "TC-E2E-014",
        "Summary":        "[P2] [e2e] sign-up new user then make purchase",
        "Description":    "New user completes registration and makes first purchase.",
        "Preconditions":  "No user with newrobot@test.com.",
        "Test Steps":     "1. Sign up as newrobot@test.com\n2. Add product to cart\n3. Pay with mock card\n4. View order history",
        "Expected Results":"Order created and visible in history for new user.",
    },
    {
        "Local Test ID":  "TC-E2E-015",
        "Summary":        "[P1] [e2e] [regression] login popup dismissed then purchase completes",
        "Description":    "After login success popup is dismissed, purchase flow is unaffected.",
        "Preconditions":  "Popups enabled.",
        "Test Steps":     "1. Sign in (popup appears)\n2. Dismiss popup\n3. Add product\n4. Buy with mock card",
        "Expected Results":"Purchase completes successfully after popup dismissal.",
    },
]


def write_excel(output_path: str) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Test Cases"

    headers = ["Local Test ID", "Summary", "Description", "Preconditions", "Test Steps", "Expected Results"]

    # Header row styling
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_fill  = PatternFill("solid", fgColor="1A1D27")
    thin_border  = Border(
        left=Side(style="thin", color="2E3350"),
        right=Side(style="thin", color="2E3350"),
        top=Side(style="thin", color="2E3350"),
        bottom=Side(style="thin", color="2E3350"),
    )

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.border    = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.row_dimensions[1].height = 24

    # Alternating row fills
    fill_a = PatternFill("solid", fgColor="1A1D27")
    fill_b = PatternFill("solid", fgColor="222536")
    font_id   = Font(bold=True, color="818CF8", size=10)
    font_reg  = Font(color="E2E8F0", size=10)
    font_mono = Font(name="Courier New", color="A0AEC0", size=9)

    for row_idx, case in enumerate(CASES, 2):
        fill = fill_a if row_idx % 2 == 0 else fill_b
        for col_idx, h in enumerate(headers, 1):
            val = case.get(h, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.fill   = fill
            cell.border = thin_border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if col_idx == 1:
                cell.font = font_id
            elif col_idx in (5, 6):
                cell.font = font_mono
            else:
                cell.font = font_reg

    # Column widths
    widths = [16, 60, 50, 40, 60, 50]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = w

    # Freeze header
    ws.freeze_panes = "A2"

    # Auto-height for data rows
    for row_idx in range(2, len(CASES) + 2):
        ws.row_dimensions[row_idx].height = 70

    wb.save(output_path)
    print(f"  OK  {len(CASES)} test cases written to {output_path}")


if __name__ == "__main__":
    out = "C:/Users/gsk54/Desktop/Robotics_Project/Test Cases/kiosk_e2e_50_tests.xlsx"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    write_excel(out)

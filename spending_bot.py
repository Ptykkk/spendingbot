import os
import re
import sqlite3
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
DB = "spending.db"
PORT = int(os.getenv("PORT", 10000))

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount REAL,
        description TEXT,
        method TEXT,
        category TEXT,
        date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        amount REAL,
        due_day INTEGER,
        method TEXT,
        notes TEXT,
        active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        principal REAL,
        interest_rate REAL,
        months INTEGER,
        total_repay REAL,
        monthly REAL,
        start_date TEXT
    )''')
    conn.commit()
    conn.close()

def safe_eval(expr):
    expr = expr.replace(" ", "")
    if not re.match(r'^[\d\.\+\-\*\/\(\)]+$', expr):
        raise ValueError("Invalid expression")
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))
    except:
        raise ValueError("Could not calculate")

def add_expense(amount, desc, method, expense_date=None, category=None):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if not expense_date:
        expense_date = date.today().isoformat()
    c.execute("INSERT INTO expenses (amount, description, method, category, date) VALUES (?,?,?,?,?)",
              (amount, desc, method.lower().strip(), category or "uncategorized", expense_date))
    conn.commit()
    conn.close()

def format_date(iso_date):
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return d.strftime("%d/%m/%Y")
    except:
        return iso_date

def parse_date(date_str):
    try:
        d = datetime.strptime(date_str, "%d/%m/%Y")
        return d.strftime("%Y-%m-%d")
    except:
        return None

def get_month_data(month_str=None):
    if not month_str:
        month_str = date.today().strftime("%Y-%m")
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT method, SUM(amount) FROM expenses WHERE date LIKE ? GROUP BY method", (month_str+"%",))
    rows = c.fetchall()
    total = sum(r[1] for r in rows)
    debit = next((r[1] for r in rows if r[0].lower().strip() == "debit"), 0)
    credit = next((r[1] for r in rows if r[0].lower().strip() == "credit"), 0)
    conn.close()
    return total, debit, credit, rows, month_str

def get_range_data(start_iso, end_iso, method_filter=None):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if method_filter:
        c.execute("SELECT method, SUM(amount) FROM expenses WHERE date >= ? AND date <= ? AND method = ? GROUP BY method",
                  (start_iso, end_iso, method_filter))
    else:
        c.execute("SELECT method, SUM(amount) FROM expenses WHERE date >= ? AND date <= ? GROUP BY method",
                  (start_iso, end_iso))
    rows = c.fetchall()
    total = sum(r[1] for r in rows)
    debit = next((r[1] for r in rows if r[0].lower().strip() == "debit"), 0)
    credit = next((r[1] for r in rows if r[0].lower().strip() == "credit"), 0)
    conn.close()
    return total, debit, credit, rows

def get_loans_monthly():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT SUM(monthly) FROM loans")
    res = c.fetchone()[0] or 0
    conn.close()
    return res

def get_bills_total():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM bills WHERE active=1")
    res = c.fetchone()[0] or 0
    conn.close()
    return res

def get_today_total():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT SUM(amount) FROM expenses WHERE date = ?", (today,))
    res = c.fetchone()[0] or 0
    conn.close()
    return res

def month_name(month_str):
    year, month = month_str.split("-")
    names = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]
    return f"{names[int(month)]} {year}"

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return  # silence logs

def run_dummy_server():
    server = HTTPServer(("0.0.0.0", PORT), DummyHandler)
    server.serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Spending tracker ready!\n\n"
        "Log like this:\n"
        "• 45.80 lunch debit\n"
        "• 3.80+13.10 coffee debit\n"
        "• 120 groceries credit 31/10/2026\n\n"
        "Commands:\n"
        "/summary — today + this month\n"
        "/today — list of today’s spends\n"
        "/month — this month full list\n"
        "/range 08/10/2026 10/11/2026 — custom date range\n"
        "/topay 08/10/2026 10/11/2026 — bills to pay (credit + loans + bills)\n"
        "/recent — last 10 entries\n"
        "/delete <id>\n"
        "/addloan ...\n"
        "/loans\n"
        "/addbill ...\n"
        "/bills"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        return

    try:
        amount = safe_eval(parts[0])
    except:
        await update.message.reply_text("Could not understand the amount. Examples:\n3.80+13.10 coffee debit\n25.50 lunch debit")
        return

    method = "debit"
    desc_parts = []
    expense_date = None

    for p in parts[1:]:
        parsed = parse_date(p)
        if parsed:
            expense_date = parsed
        elif p.lower().strip() in ("debit", "credit", "cash", "transfer", "loan"):
            method = p.lower().strip()
        else:
            desc_parts.append(p)

    description = " ".join(desc_parts) or "expense"
    add_expense(amount, description, method, expense_date)

    display_date = format_date(expense_date) if expense_date else format_date(date.today().isoformat())
    today = get_today_total()
    total, debit, credit, _, month_str = get_month_data()
    mname = month_name(month_str)

    reply = (
        f"✅ Logged ${amount:,.2f} – {description} ({method})\n"
        f"Date: {display_date}\n\n"
        f"📅 Today: ${today:,.2f}\n\n"
        f"📊 {mname}\n"
        f"Total: ${total:,.2f}\n"
        f"Debit: ${debit:,.2f}\n"
        f"Credit: ${credit:,.2f}"
    )
    await update.message.reply_text(reply)

async def range_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage:\n/range 08/10/2026 10/11/2026\n/range 08/10/2026 10/11/2026 credit")
        return

    start = parse_date(args[0])
    end = parse_date(args[1])
    if not start or not end:
        await update.message.reply_text("Dates must be in DD/MM/YYYY format.")
        return

    method_filter = args[2].lower() if len(args) > 2 else None
    total, debit, credit, _ = get_range_data(start, end, method_filter)

    title = f"{format_date(start)} → {format_date(end)}"
    if method_filter:
        title += f" ({method_filter})"

    if method_filter:
        reply = f"📊 {title}\nTotal: ${total:,.2f}"
    else:
        reply = (
            f"📊 {title}\n"
            f"Total: ${total:,.2f}\n"
            f"Debit: ${debit:,.2f}\n"
            f"Credit: ${credit:,.2f}"
        )
    await update.message.reply_text(reply)

async def topay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /topay 08/10/2026 10/11/2026")
        return

    start = parse_date(args[0])
    end = parse_date(args[1])
    if not start or not end:
        await update.message.reply_text("Dates must be in DD/MM/YYYY format.")
        return

    _, debit, credit, _ = get_range_data(start, end)
    loans = get_loans_monthly()
    bills = get_bills_total()
    still_to_pay = credit + loans + bills

    reply = (
        f"💰 Bills to pay ({format_date(start)} → {format_date(end)})\n\n"
        f"Credit card: ${credit:,.2f}\n"
        f"Loans: ${loans:,.2f}\n"
        f"Other bills: ${bills:,.2f}\n"
        f"━━━━━━━━━━━━━━\n"
        f"**Total still to pay: ${still_to_pay:,.2f}**\n\n"
        f"Debit already paid: ${debit:,.2f}"
    )
    await update.message.reply_text(reply)

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_today_total()
    total, debit, credit, _, month_str = get_month_data()
    mname = month_name(month_str)
    await update.message.reply_text(
        f"📅 Today ({format_date(date.today().isoformat())}): ${today:,.2f}\n\n"
        f"📊 {mname}\n"
        f"Total: ${total:,.2f}\n"
        f"Debit: ${debit:,.2f}\n"
        f"Credit: ${credit:,.2f}"
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    today_iso = date.today().isoformat()
    c.execute("SELECT id, description, amount, method FROM expenses WHERE date = ? ORDER BY id DESC", (today_iso,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No spends logged today.")
        return
    lines = [f"{r[0]}. {r[1]} — ${r[2]:,.2f} ({r[3]})" for r in rows]
    total = sum(r[2] for r in rows)
    await update.message.reply_text(
        f"📅 Today ({format_date(today_iso)}) — Total: ${total:,.2f}\n\n" + "\n".join(lines)
    )

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_str = context.args[0] if context.args else date.today().strftime("%Y-%m")
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT description, amount, method, date FROM expenses WHERE date LIKE ? ORDER BY date DESC", (month_str+"%",))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"No expenses found for {month_name(month_str)}.")
        return

    total, debit, credit, _, _ = get_month_data(month_str)
    lines = [f"{format_date(r[3])}  ${r[1]:,.2f}  {r[0]} ({r[2]})" for r in rows[:30]]
    text = (
        f"📊 {month_name(month_str)}\n"
        f"Total: ${total:,.2f} | Debit: ${debit:,.2f} | Credit: ${credit:,.2f}\n\n"
        + "\n".join(lines)
    )
    if len(rows) > 30:
        text += "\n\n… (showing latest 30)"
    await update.message.reply_text(text)

async def months(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT DISTINCT substr(date,1,7) FROM expenses ORDER BY 1 DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No data yet.")
        return
    lines = [f"• {month_name(r[0])} → /month {r[0]}" for r in rows]
    await update.message.reply_text("Available months:\n\n" + "\n".join(lines))

async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, description, amount, method, date FROM expenses ORDER BY id DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No entries yet.")
        return
    lines = [f"{r[0]}. {format_date(r[4])}  ${r[2]:,.2f}  {r[1]} ({r[3]})" for r in rows]
    await update.message.reply_text("Last 10 entries (use /delete <id>):\n\n" + "\n".join(lines))

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete 5")
        return
    try:
        exp_id = int(context.args[0])
    except:
        await update.message.reply_text("Please give a number, e.g. /delete 5")
        return
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id = ?", (exp_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted:
        await update.message.reply_text(f"✅ Deleted entry #{exp_id}")
    else:
        await update.message.reply_text("Entry not found.")

async def add_loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 4:
        await update.message.reply_text("Usage: /addloan DBS 13000 4.5 12")
        return
    name = args[0]
    principal = float(args[1])
    rate = float(args[2])
    months = int(args[3])
    total_repay = principal * (1 + rate / 100)
    monthly = total_repay / months
    start = date.today().isoformat()

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO loans (name, principal, interest_rate, months, total_repay, monthly, start_date) VALUES (?,?,?,?,?,?,?)",
              (name, principal, rate, months, total_repay, monthly, start))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Loan added: {name}\n"
        f"Principal: ${principal:,.2f}\n"
        f"Interest: {rate}%\n"
        f"Total to repay: ${total_repay:,.2f}\n"
        f"Monthly: ${monthly:,.2f} over {months} months"
    )

async def loans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT name, principal, interest_rate, months, total_repay, monthly FROM loans")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No loans added. Use /addloan")
        return
    lines = []
    for r in rows:
        lines.append(
            f"• {r[0]}\n"
            f"  Principal: ${r[1]:,.2f} | Interest: {r[2]}%\n"
            f"  Total repay: ${r[4]:,.2f} | Monthly: ${r[5]:,.2f} ({r[3]} months)"
        )
    await update.message.reply_text("Loans:\n\n" + "\n\n".join(lines))

async def add_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: /addbill rent 1800 5")
        return
    name, amount, due_day = args[0], float(args[1]), int(args[2])
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO bills (name, amount, due_day, method) VALUES (?,?,?,?)",
              (name, amount, due_day, "bill"))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Bill added: {name} ${amount:,.2f} due on the {due_day}th")

async def bills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT name, amount, due_day FROM bills WHERE active=1 ORDER BY due_day")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No bills added yet. Use /addbill")
        return
    lines = [f"• {r[0]} — ${r[1]:,.2f} (due {r[2]}th)" for r in rows]
    total = sum(r[1] for r in rows)
    await update.message.reply_text(f"📅 Upcoming bills (total ~${total:,.2f}/month):\n\n" + "\n".join(lines))

def main():
    init_db()
    # Start dummy server so Render detects a port
    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("months", months))
    app.add_handler(CommandHandler("range", range_summary))
    app.add_handler(CommandHandler("topay", topay))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("addloan", add_loan))
    app.add_handler(CommandHandler("loans", loans))
    app.add_handler(CommandHandler("addbill", add_bill))
    app.add_handler(CommandHandler("bills", bills))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

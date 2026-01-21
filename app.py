import os
import re
import json
from datetime import datetime
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Environment variables
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '1CRWaO855R-_8GKR2Pwpqw28ZFWKAMVAY4F6ZhFfEnbQ')

# Initialize Slack client
slack_client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# Initialize Google Sheets
def get_gsheet_client():
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ])
        return gspread.authorize(creds)
    return None

def get_sheet(sheet_name):
    gc = get_gsheet_client()
    if gc:
        spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
        return spreadsheet.worksheet(sheet_name)
    return None

def get_transaction_sheet():
    return get_sheet('Transaction')

def get_fixed_bills_sheet():
    return get_sheet('Fixed Bills')

# Parse amount (handles: 2.8M, 2800000, 2,800,000, 150K)
def parse_amount(amount_str):
    amount_str = amount_str.replace(',', '').replace('₩', '').strip()
    if 'M' in amount_str.upper():
        return int(float(amount_str.upper().replace('M', '')) * 1000000)
    elif 'K' in amount_str.upper():
        return int(float(amount_str.upper().replace('K', '')) * 1000)
    return int(float(amount_str))

# Format currency
def fmt(amount):
    if amount >= 1000000:
        return f"₩{amount/1000000:.1f}M"
    elif amount >= 1000:
        return f"₩{amount/1000:.0f}K"
    return f"₩{amount:,.0f}"

# Get all fixed bills as a dictionary for quick lookup
def get_fixed_bills_dict():
    sheet = get_fixed_bills_sheet()
    if not sheet:
        return {}
    
    records = sheet.get_all_records()
    bills = {}
    
    for row in records:
        category = row.get('Category', '')
        if category and row.get('Status') == 'Active':
            # Create lookup key (lowercase, simplified)
            key = category.lower().strip()
            # Also create simplified keys for matching
            simple_key = key.split(' - ')[0].split(' ')[0]  # "Phone - Jacob" -> "phone"
            
            bills[key] = {
                'category': category,
                'amount': row.get('Amount', 0),
                'type': row.get('Type', 'Personal'),
                'person': row.get('Person', 'Joint'),
                'auto_include': row.get('Auto_Include', 'No')
            }
            # Add simplified key too
            if simple_key != key:
                bills[simple_key] = bills[key]
    
    return bills

# Find matching fixed bill category
def find_fixed_bill(text):
    bills = get_fixed_bills_dict()
    text_lower = text.lower().strip()
    
    # Direct match
    if text_lower in bills:
        return bills[text_lower]
    
    # Partial match - check if any bill category contains the text
    for key, bill in bills.items():
        if text_lower in key or key in text_lower:
            return bill
    
    # Common aliases
    aliases = {
        'gas': 'gas',
        'điện': 'electricity',
        'electric': 'electricity',
        'electricity': 'electricity',
        'internet': 'internet',
        'wifi': 'internet',
        'rent': 'rent',
        'nhà': 'rent',
        'phone': 'phone',
        'điện thoại': 'phone',
        'groceries': 'groceries',
        'grocery': 'groceries',
        'đi chợ': 'groceries',
        'eating': 'eating out',
        'ăn ngoài': 'eating out',
        'dinner': 'eating out',
        'transport': 'transport',
        'đi lại': 'transport',
        'netflix': 'disney +',
        'disney': 'disney +',
        'youtube': 'youtube premium',
        'claude': 'claude pro',
        'chatgpt': 'chatgpt pro',
        'microsoft': 'microsoft 365',
        'canva': 'canva pro',
        'icloud': 'icloud storage',
        'insurance': 'health insurance',
        'bảo hiểm': 'health insurance',
    }
    
    if text_lower in aliases:
        alias_key = aliases[text_lower]
        for key, bill in bills.items():
            if alias_key in key:
                return bill
    
    return None

# Parse message for transaction
def parse_transaction(text, user_name):
    text = text.strip()
    
    # Pattern: "jacob 2.8M salary" or "naomi 5M commission" or "joint 500K groceries"
    # Also: "2.8M salary" (defaults to person who sent it)
    # Also: "gas 150K" (fixed bill pattern)
    
    patterns = [
        # With person specified: "jacob 2.8M salary"
        r'^(jacob|naomi|joint)\s+([\d.,]+[mk]?)\s+(.+)$',
        # Without person: "2.8M salary" 
        r'^([\d.,]+[mk]?)\s+(.+)$',
        # Fixed bill style: "gas 150K" or "electricity 80000"
        r'^([a-zA-Z\s]+)\s+([\d.,]+[mk]?)$',
    ]
    
    person = user_name  # Default to sender
    amount = None
    description = None
    fixed_bill = None
    
    # Try pattern with person: "jacob 2.8M salary"
    match = re.match(patterns[0], text, re.IGNORECASE)
    if match:
        person = match.group(1).capitalize()
        amount = parse_amount(match.group(2))
        description = match.group(3).strip()
    else:
        # Try fixed bill style: "gas 150K"
        match = re.match(patterns[2], text, re.IGNORECASE)
        if match:
            potential_category = match.group(1).strip()
            fixed_bill = find_fixed_bill(potential_category)
            if fixed_bill:
                amount = parse_amount(match.group(2))
                description = fixed_bill['category']
                person = fixed_bill['person'] if fixed_bill['person'] != 'Both' else 'Joint'
        
        # Try pattern without person: "2.8M salary"
        if not amount:
            match = re.match(patterns[1], text, re.IGNORECASE)
            if match:
                amount = parse_amount(match.group(1))
                description = match.group(2).strip()
    
    if amount and description:
        # Determine type based on keywords
        income_keywords = ['salary', 'commission', 'bonus', 'income', 'fee', 'revenue', 'lương', 'hoa hồng', 'thưởng']
        
        tx_type = 'Expense'  # Default
        for kw in income_keywords:
            if kw in description.lower():
                tx_type = 'Income'
                break
        
        return {
            'person': person,
            'amount': amount,
            'description': description,
            'type': tx_type,
            'fixed_bill': fixed_bill
        }
    
    return None

# Log transaction to Google Sheet
def log_transaction(tx_data):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    now = datetime.now()
    month_start = now.strftime('%Y-%m-01')
    
    row = [
        now.strftime('%Y-%m-%d'),           # Date
        tx_data['type'],                     # Type
        tx_data['description'],              # Category
        tx_data['amount'],                   # Amount
        tx_data['description'],              # Description
        tx_data['person'],                   # Person
        month_start,                         # Month
        'slack'                              # Source
    ]
    
    sheet.append_row(row)
    return True, "Transaction logged!"

# Build response message for fixed bill
def build_fixed_bill_response(tx_data):
    fixed_bill = tx_data.get('fixed_bill')
    amount = tx_data['amount']
    category = tx_data['description']
    
    response = f"✅ Logged: {category} {fmt(amount)}\n"
    
    if fixed_bill:
        default_amount = fixed_bill['amount']
        if default_amount > 0:
            ratio = amount / default_amount
            diff = amount - default_amount
            
            if ratio > 2:
                response += f"📊 Note: Usually {fmt(default_amount)} - this is {ratio:.0f}x higher!\n"
                # Add contextual hint based on category
                if 'gas' in category.lower():
                    response += "🔥 Winter heating? (Default unchanged)"
                elif 'electric' in category.lower():
                    response += "❄️ AC or heating? (Default unchanged)"
                elif 'groceries' in category.lower():
                    response += "🛒 Big shopping trip? (Default unchanged)"
                else:
                    response += "(Default unchanged for future months)"
            elif ratio > 1.2:
                response += f"📊 Note: {fmt(diff)} more than usual ({fmt(default_amount)})"
            elif ratio < 0.5:
                response += f"📊 Note: Usually {fmt(default_amount)} - nice savings! 🎉"
            elif ratio < 0.8:
                response += f"📊 Note: {fmt(-diff)} less than usual ({fmt(default_amount)})"
    
    return response

# Get fund balances (latest for each fund)
def get_fund_status():
    sheet = get_transaction_sheet()
    if not sheet:
        return None
    
    records = sheet.get_all_records()
    
    funds = {}
    for row in records:
        if row.get('Type') == 'Fund Balance':
            fund_name = row.get('Category', '')
            funds[fund_name] = {
                'amount': row.get('Amount', 0),
                'date': row.get('Date', '')
            }
    
    return funds

# Get monthly summary
def get_monthly_summary(month=None):
    sheet = get_transaction_sheet()
    if not sheet:
        return None
    
    if not month:
        month = datetime.now().strftime('%Y-%m-01')
    
    records = sheet.get_all_records()
    
    income = {'Jacob': 0, 'Naomi': 0, 'Joint': 0}
    expenses = {'Jacob': 0, 'Naomi': 0, 'Joint': 0}
    
    for row in records:
        row_month = str(row.get('Month', ''))[:10]
        if row_month == month:
            person = row.get('Person', 'Joint')
            amount = row.get('Amount', 0) or 0
            tx_type = row.get('Type', '')
            
            if tx_type == 'Income':
                income[person] = income.get(person, 0) + amount
            elif tx_type == 'Expense':
                expenses[person] = expenses.get(person, 0) + amount
    
    return {
        'month': month,
        'income': income,
        'expenses': expenses,
        'total_income': sum(income.values()),
        'total_expenses': sum(expenses.values())
    }

# Get fixed bills total
def get_fixed_bills_total():
    bills = get_fixed_bills_dict()
    total = sum(b['amount'] for b in bills.values() if b.get('auto_include') == 'Yes')
    return total

# Handle Slack events
@app.route('/slack/events', methods=['POST'])
def slack_events():
    if not signature_verifier.is_valid_request(request.get_data(), request.headers):
        return jsonify({'error': 'invalid request'}), 403
    
    data = request.json
    
    if data.get('type') == 'url_verification':
        return jsonify({'challenge': data.get('challenge')})
    
    event = data.get('event', {})
    event_type = event.get('type')
    
    if event.get('bot_id'):
        return jsonify({'ok': True})
    
    if event_type == 'message':
        channel = event.get('channel')
        text = event.get('text', '').strip()
        user_id = event.get('user')
        
        # Get user info
        try:
            user_info = slack_client.users_info(user=user_id)
            user_name = user_info['user']['real_name'].split()[0]
            if 'naomi' in user_name.lower() or 'nao' in user_name.lower():
                user_name = 'Naomi'
            else:
                user_name = 'Jacob'
        except:
            user_name = 'Jacob'
        
        # Command: status
        if text.lower() in ['status', 'tình hình', 'báo cáo', 'check']:
            funds = get_fund_status()
            summary = get_monthly_summary()
            fixed_total = get_fixed_bills_total()
            
            if funds or summary:
                msg = "📊 *Status Update*\n\n"
                
                if summary:
                    month_name = datetime.strptime(summary['month'], '%Y-%m-%d').strftime('%B %Y')
                    msg += f"*{month_name}:*\n"
                    msg += f"• Income: {fmt(summary['total_income'])}\n"
                    msg += f"• Expenses: {fmt(summary['total_expenses'])}\n"
                    msg += f"• Fixed Bills (default): {fmt(fixed_total)}\n"
                    net = summary['total_income'] - summary['total_expenses']
                    msg += f"• Net: {fmt(net)}\n\n"
                
                if funds:
                    msg += "*Fund Balances:*\n"
                    for fund, data in funds.items():
                        msg += f"• {fund}: {fmt(data['amount'])}\n"
                    
                    # Calculate progress to 15M
                    emergency = funds.get('Emergency Fund', {}).get('amount', 0)
                    if emergency:
                        progress = (emergency / 15000000) * 100
                        msg += f"\n🎯 Emergency Fund: {progress:.1f}% → ₩15M"
                
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text="❌ Cannot fetch status")
        
        # Command: bills or fixed
        elif text.lower() in ['bills', 'fixed', 'fixed bills', 'chi phí cố định']:
            bills = get_fixed_bills_dict()
            total = 0
            msg = "📋 *Fixed Bills (Active):*\n\n"
            
            # Group by person
            jacob_bills = []
            naomi_bills = []
            joint_bills = []
            
            seen = set()  # Avoid duplicates from aliases
            for key, bill in bills.items():
                cat = bill['category']
                if cat in seen:
                    continue
                seen.add(cat)
                
                amt = bill['amount']
                total += amt
                line = f"• {cat}: {fmt(amt)}"
                
                if bill['person'] == 'Jacob':
                    jacob_bills.append(line)
                elif bill['person'] == 'Naomi':
                    naomi_bills.append(line)
                else:
                    joint_bills.append(line)
            
            if joint_bills:
                msg += "*Joint:*\n" + "\n".join(joint_bills) + "\n\n"
            if jacob_bills:
                msg += "*Jacob:*\n" + "\n".join(jacob_bills) + "\n\n"
            if naomi_bills:
                msg += "*Naomi:*\n" + "\n".join(naomi_bills) + "\n\n"
            
            msg += f"*Total: {fmt(total)}*"
            
            slack_client.chat_postMessage(channel=channel, text=msg)
        
        # Command: help
        elif text.lower() in ['help', 'trợ giúp', '?']:
            help_msg = """🤖 *Finance Bot Commands:*

*Log Income/Expense:*
• `jacob 2.8M salary` - Log Jacob's income
• `naomi 5M commission` - Log Naomi's income
• `joint 500K groceries` - Log joint expense
• `2.8M salary` - Log for yourself

*Log Fixed Bills (with smart comparison):*
• `gas 150K` - Log gas bill
• `electricity 80K` - Log electricity
• `groceries 600K` - Log groceries

*Check Status:*
• `status` - See fund balances & monthly summary
• `bills` - See all fixed bills

*Amount formats:*
• `2.8M` = ₩2,800,000
• `500K` = ₩500,000
• `2800000` = ₩2,800,000"""
            slack_client.chat_postMessage(channel=channel, text=help_msg)
        
        # Try to parse as transaction
        else:
            tx = parse_transaction(text, user_name)
            if tx:
                success, msg = log_transaction(tx)
                if success:
                    if tx.get('fixed_bill'):
                        response = build_fixed_bill_response(tx)
                    else:
                        response = f"✅ Logged: {tx['type']} - {tx['person']} - {fmt(tx['amount'])} - {tx['description']}"
                else:
                    response = f"❌ Error: {msg}"
                slack_client.chat_postMessage(channel=channel, text=response)
    
    return jsonify({'ok': True})

# Health check
@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Couple Finance Bot v2'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

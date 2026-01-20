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

def get_transaction_sheet():
    gc = get_gsheet_client()
    if gc:
        spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
        return spreadsheet.worksheet('Transaction')
    return None

# Parse amount (handles: 2.8M, 2800000, 2,800,000)
def parse_amount(amount_str):
    amount_str = amount_str.replace(',', '').replace('₩', '').strip()
    if 'M' in amount_str.upper():
        return int(float(amount_str.upper().replace('M', '')) * 1000000)
    elif 'K' in amount_str.upper():
        return int(float(amount_str.upper().replace('K', '')) * 1000)
    return int(float(amount_str))

# Parse message for transaction
def parse_transaction(text, user_name):
    text = text.strip().lower()
    
    # Pattern: "jacob 2.8M salary" or "naomi 5M commission" or "joint 500K groceries"
    # Also: "2.8M salary" (defaults to person who sent it)
    
    patterns = [
        # With person specified: "jacob 2.8M salary"
        r'^(jacob|naomi|joint)\s+([\d.,]+[mk]?)\s+(.+)$',
        # Without person: "2.8M salary" 
        r'^([\d.,]+[mk]?)\s+(.+)$',
    ]
    
    person = user_name  # Default to sender
    amount = None
    description = None
    
    # Try pattern with person
    match = re.match(patterns[0], text, re.IGNORECASE)
    if match:
        person = match.group(1).capitalize()
        amount = parse_amount(match.group(2))
        description = match.group(3).strip()
    else:
        # Try pattern without person
        match = re.match(patterns[1], text, re.IGNORECASE)
        if match:
            amount = parse_amount(match.group(1))
            description = match.group(2).strip()
    
    if amount and description:
        # Determine type based on keywords
        income_keywords = ['salary', 'commission', 'bonus', 'income', 'fee', 'lương', 'hoa hồng']
        expense_keywords = ['buy', 'bought', 'purchase', 'spent', 'paid', 'mua', 'chi', 'trả']
        
        tx_type = 'Expense'  # Default
        for kw in income_keywords:
            if kw in description.lower():
                tx_type = 'Income'
                break
        
        return {
            'person': person,
            'amount': amount,
            'description': description,
            'type': tx_type
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
        tx_data['description'].title(),      # Category
        tx_data['amount'],                   # Amount
        tx_data['description'],              # Description
        tx_data['person'],                   # Person
        month_start,                         # Month
        'slack'                              # Source
    ]
    
    sheet.append_row(row)
    return True, "Transaction logged!"

# Get fund balances (latest for each fund)
def get_fund_status():
    sheet = get_transaction_sheet()
    if not sheet:
        return None
    
    records = sheet.get_all_records()
    
    # Find latest fund balances
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

# Format currency
def fmt(amount):
    return f"₩{amount:,.0f}"

# Handle Slack events
@app.route('/slack/events', methods=['POST'])
def slack_events():
    # Verify request
    if not signature_verifier.is_valid_request(request.get_data(), request.headers):
        return jsonify({'error': 'invalid request'}), 403
    
    data = request.json
    
    # URL verification challenge
    if data.get('type') == 'url_verification':
        return jsonify({'challenge': data.get('challenge')})
    
    # Handle events
    event = data.get('event', {})
    event_type = event.get('type')
    
    # Ignore bot messages
    if event.get('bot_id'):
        return jsonify({'ok': True})
    
    if event_type == 'message':
        channel = event.get('channel')
        text = event.get('text', '').strip()
        user_id = event.get('user')
        
        # Get user info to determine Jacob or Naomi
        try:
            user_info = slack_client.users_info(user=user_id)
            user_name = user_info['user']['real_name'].split()[0]
            # Map to Jacob or Naomi based on name/email
            if 'naomi' in user_name.lower() or 'nao' in user_name.lower():
                user_name = 'Naomi'
            else:
                user_name = 'Jacob'
        except:
            user_name = 'Jacob'
        
        # Command: status
        if text.lower() in ['status', 'tình hình', 'báo cáo']:
            funds = get_fund_status()
            summary = get_monthly_summary()
            
            if funds:
                msg = f"📊 *Fund Balances:*\n"
                for fund, data in funds.items():
                    msg += f"• {fund}: {fmt(data['amount'])}\n"
                
                if summary:
                    msg += f"\n📅 *This Month ({summary['month'][:7]}):*\n"
                    msg += f"• Income: {fmt(summary['total_income'])}\n"
                    msg += f"• Expenses: {fmt(summary['total_expenses'])}\n"
                    msg += f"• Net: {fmt(summary['total_income'] - summary['total_expenses'])}"
                
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text="❌ Cannot fetch status")
        
        # Command: help
        elif text.lower() in ['help', 'trợ giúp', '?']:
            help_msg = """🤖 *Finance Bot Commands:*

*Log Income/Expense:*
• `jacob 2.8M salary` - Log Jacob's income
• `naomi 5M commission` - Log Naomi's income  
• `joint 500K groceries` - Log joint expense
• `2.8M salary` - Log for yourself

*Check Status:*
• `status` - See fund balances & monthly summary

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
                    response = f"✅ Logged: {tx['type']} - {tx['person']} - {fmt(tx['amount'])} - {tx['description']}"
                else:
                    response = f"❌ Error: {msg}"
                slack_client.chat_postMessage(channel=channel, text=response)
    
    return jsonify({'ok': True})

# Health check
@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Couple Finance Bot'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

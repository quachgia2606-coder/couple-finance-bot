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

# Store last deleted transaction for undo (in memory - resets on restart)
last_deleted = {}

# Store last list results for delete by number
last_list_results = {}

# Month name mappings
MONTH_NAMES = {
    'jan': 1, 'january': 1, 'thg1': 1,
    'feb': 2, 'february': 2, 'thg2': 2,
    'mar': 3, 'march': 3, 'thg3': 3,
    'apr': 4, 'april': 4, 'thg4': 4,
    'may': 5, 'thg5': 5,
    'jun': 6, 'june': 6, 'thg6': 6,
    'jul': 7, 'july': 7, 'thg7': 7,
    'aug': 8, 'august': 8, 'thg8': 8,
    'sep': 9, 'sept': 9, 'september': 9, 'thg9': 9,
    'oct': 10, 'october': 10, 'thg10': 10,
    'nov': 11, 'november': 11, 'thg11': 11,
    'dec': 12, 'december': 12, 'thg12': 12,
}

MONTH_NAMES_REVERSE = {
    1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
    7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
}

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

# Parse amount
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

# Parse month from text
def parse_month(text):
    text = text.lower().strip()
    now = datetime.now()
    
    match = re.match(r'^(\d{4})-(\d{1,2})$', text)
    if match:
        return int(match.group(1)), int(match.group(2))
    
    match = re.match(r'^(\d{1,2})/(\d{4})$', text)
    if match:
        return int(match.group(2)), int(match.group(1))
    
    if text in MONTH_NAMES:
        month = MONTH_NAMES[text]
        year = now.year
        if month > now.month:
            year -= 1
        return year, month
    
    return None

# Extract month from text
def extract_month_from_text(text):
    words = text.split()
    now = datetime.now()
    
    for i, word in enumerate(words):
        month_info = parse_month(word)
        if month_info:
            year, month = month_info
            cleaned_words = words[:i] + words[i+1:]
            cleaned_text = ' '.join(cleaned_words)
            is_backdated = not (year == now.year and month == now.month)
            return cleaned_text, year, month, is_backdated
    
    return text, now.year, now.month, False

# Get fixed bills dictionary
def get_fixed_bills_dict():
    sheet = get_fixed_bills_sheet()
    if not sheet:
        return {}
    
    records = sheet.get_all_records()
    bills = {}
    
    for row in records:
        category = row.get('Category', '')
        if category and row.get('Status') == 'Active':
            key = category.lower().strip()
            simple_key = key.split(' - ')[0].split(' ')[0]
            
            bills[key] = {
                'category': category,
                'amount': row.get('Amount', 0),
                'type': row.get('Type', 'Personal'),
                'person': row.get('Person', 'Joint'),
                'auto_include': row.get('Auto_Include', 'No')
            }
            if simple_key != key:
                bills[simple_key] = bills[key]
    
    return bills

# Find matching fixed bill
def find_fixed_bill(text):
    bills = get_fixed_bills_dict()
    text_lower = text.lower().strip()
    
    if text_lower in bills:
        return bills[text_lower]
    
    for key, bill in bills.items():
        if text_lower in key or key in text_lower:
            return bill
    
    aliases = {
        'gas': 'gas', 'điện': 'electricity', 'electric': 'electricity',
        'electricity': 'electricity', 'internet': 'internet', 'wifi': 'internet',
        'rent': 'rent', 'nhà': 'rent', 'phone': 'phone', 'điện thoại': 'phone',
        'groceries': 'groceries', 'grocery': 'groceries', 'đi chợ': 'groceries',
        'eating': 'eating out', 'ăn ngoài': 'eating out', 'dinner': 'eating out',
        'transport': 'transport', 'đi lại': 'transport', 'disney': 'disney +',
        'youtube': 'youtube premium', 'claude': 'claude pro',
        'chatgpt': 'chatgpt pro', 'microsoft': 'microsoft 365', 'canva': 'canva pro',
        'icloud': 'icloud storage', 'insurance': 'health insurance', 'bảo hiểm': 'health insurance',
    }
    
    if text_lower in aliases:
        alias_key = aliases[text_lower]
        for key, bill in bills.items():
            if alias_key in key:
                return bill
    
    return None

# Parse transaction
def parse_transaction(text, user_name):
    text = text.strip()
    cleaned_text, year, month, is_backdated = extract_month_from_text(text)
    
    patterns = [
        r'^(jacob|naomi|joint)\s+([\d.,]+[mk]?)\s+(.+)$',
        r'^([\d.,]+[mk]?)\s+(.+)$',
        r'^([a-zA-Z\s]+)\s+([\d.,]+[mk]?)$',
    ]
    
    person = user_name
    amount = None
    description = None
    fixed_bill = None
    
    match = re.match(patterns[0], cleaned_text, re.IGNORECASE)
    if match:
        person = match.group(1).capitalize()
        amount = parse_amount(match.group(2))
        description = match.group(3).strip()
    else:
        match = re.match(patterns[2], cleaned_text, re.IGNORECASE)
        if match:
            potential_category = match.group(1).strip()
            fixed_bill = find_fixed_bill(potential_category)
            if fixed_bill:
                amount = parse_amount(match.group(2))
                description = fixed_bill['category']
                person = fixed_bill['person'] if fixed_bill['person'] != 'Both' else 'Joint'
        
        if not amount:
            match = re.match(patterns[1], cleaned_text, re.IGNORECASE)
            if match:
                amount = parse_amount(match.group(1))
                description = match.group(2).strip()
    
    if amount and description:
        income_keywords = ['salary', 'commission', 'bonus', 'income', 'fee', 'revenue', 'lương', 'hoa hồng', 'thưởng']
        
        tx_type = 'Expense'
        for kw in income_keywords:
            if kw in description.lower():
                tx_type = 'Income'
                break
        
        return {
            'person': person,
            'amount': amount,
            'description': description,
            'type': tx_type,
            'fixed_bill': fixed_bill,
            'year': year,
            'month': month,
            'is_backdated': is_backdated
        }
    
    return None

# Log transaction
def log_transaction(tx_data):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    year = tx_data.get('year', datetime.now().year)
    month = tx_data.get('month', datetime.now().month)
    
    if tx_data.get('is_backdated'):
        date_str = f"{year}-{month:02d}-15"
    else:
        date_str = datetime.now().strftime('%Y-%m-%d')
    
    month_start = f"{year}-{month:02d}-01"
    
    row = [
        date_str,
        tx_data['type'],
        tx_data['description'],
        tx_data['amount'],
        tx_data['description'],
        tx_data['person'],
        month_start,
        'slack'
    ]
    
    sheet.append_row(row)
    return True, "Transaction logged!"

# Get all transactions
def get_all_transactions():
    sheet = get_transaction_sheet()
    if not sheet:
        return []
    
    records = sheet.get_all_records()
    transactions = []
    
    for i, row in enumerate(records):
        tx_type = row.get('Type', '')
        if tx_type in ['Income', 'Expense']:
            transactions.append({
                'row_index': i + 2,  # +2 because header is row 1, and gspread is 1-indexed
                'date': row.get('Date', ''),
                'type': tx_type,
                'category': row.get('Category', ''),
                'amount': row.get('Amount', 0),
                'description': row.get('Description', ''),
                'person': row.get('Person', ''),
                'month': row.get('Month', ''),
                'source': row.get('Source', '')
            })
    
    return transactions

# Filter transactions
def filter_transactions(transactions, filter_type=None, filter_category=None, filter_person=None, filter_month=None, limit=None):
    filtered = transactions
    
    if filter_type:
        filtered = [t for t in filtered if t['type'].lower() == filter_type.lower()]
    
    if filter_category:
        filtered = [t for t in filtered if filter_category.lower() in t['category'].lower()]
    
    if filter_person:
        filtered = [t for t in filtered if t['person'].lower() == filter_person.lower()]
    
    if filter_month:
        filtered = [t for t in filtered if t['month'][:7] == filter_month]
    
    # Sort by date descending (newest first)
    filtered = sorted(filtered, key=lambda x: x['date'], reverse=True)
    
    if limit:
        filtered = filtered[:limit]
    
    return filtered

# Parse list command
def parse_list_command(text):
    """
    Parse: list, list dec, list gas, list gas 5, list expense, list jacob dec
    Returns: (filter_type, filter_category, filter_person, filter_month, limit)
    """
    words = text.lower().split()[1:]  # Remove 'list'
    
    filter_type = None
    filter_category = None
    filter_person = None
    filter_month = None
    limit = None
    
    now = datetime.now()
    
    for word in words:
        # Check if it's a number (limit)
        if word.isdigit():
            limit = int(word)
        # Check if it's a month
        elif word in MONTH_NAMES:
            month_num = MONTH_NAMES[word]
            year = now.year if month_num <= now.month else now.year - 1
            filter_month = f"{year}-{month_num:02d}"
        # Check if it's a type
        elif word in ['income', 'expense']:
            filter_type = word.capitalize()
        # Check if it's a person
        elif word in ['jacob', 'naomi', 'joint']:
            filter_person = word.capitalize()
        # Otherwise it's a category
        else:
            filter_category = word
    
    # Default: if no month specified and no category, show current month
    if not filter_month and not filter_category and not limit:
        filter_month = now.strftime('%Y-%m')
    
    return filter_type, filter_category, filter_person, filter_month, limit

# Format transaction list
def format_transaction_list(transactions, title, channel_id):
    if not transactions:
        return "📋 No transactions found."
    
    # Store for later delete/edit reference
    last_list_results[channel_id] = transactions
    
    msg = f"📋 *{title}:*\n\n"
    total = 0
    
    for i, tx in enumerate(transactions, 1):
        date_str = tx['date'][:10]
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            date_display = date_obj.strftime('%b %d')
        except:
            date_display = date_str
        
        emoji = "💵" if tx['type'] == 'Income' else "💸"
        amount = tx['amount'] or 0
        total += amount if tx['type'] == 'Income' else -amount
        
        msg += f"{i}. {emoji} {date_display} | {tx['category']} | {fmt(amount)} | {tx['person']}\n"
    
    msg += f"\n*To delete: * `delete 1` or `delete last`"
    msg += f"\n*To edit amount: * `edit 1 150K`"
    
    return msg

# Delete transaction by row index
def delete_transaction(row_index, channel_id):
    global last_deleted
    
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    # Get the row data before deleting (for undo)
    try:
        row_data = sheet.row_values(row_index)
        last_deleted[channel_id] = {
            'row_data': row_data,
            'timestamp': datetime.now()
        }
        
        sheet.delete_rows(row_index)
        return True, row_data
    except Exception as e:
        return False, str(e)

# Undo delete
def undo_delete(channel_id):
    global last_deleted
    
    if channel_id not in last_deleted:
        return False, "Nothing to undo"
    
    deleted_info = last_deleted[channel_id]
    
    # Check if undo is within 5 minutes
    time_diff = (datetime.now() - deleted_info['timestamp']).seconds
    if time_diff > 300:
        return False, "Undo expired (>5 minutes)"
    
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        sheet.append_row(deleted_info['row_data'])
        del last_deleted[channel_id]
        return True, deleted_info['row_data']
    except Exception as e:
        return False, str(e)

# Edit transaction
def edit_transaction(row_index, new_amount):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        # Amount is in column D (4th column)
        old_value = sheet.cell(row_index, 4).value
        sheet.update_cell(row_index, 4, new_amount)
        return True, old_value
    except Exception as e:
        return False, str(e)

# Build response for fixed bill
def build_fixed_bill_response(tx_data):
    fixed_bill = tx_data.get('fixed_bill')
    amount = tx_data['amount']
    category = tx_data['description']
    is_backdated = tx_data.get('is_backdated', False)
    year = tx_data.get('year')
    month = tx_data.get('month')
    
    response = f"✅ Logged: {category} {fmt(amount)}\n"
    
    if is_backdated:
        month_name = f"{MONTH_NAMES_REVERSE[month]} {year}"
        response += f"📅 {month_name} (backdated)\n"
    
    if fixed_bill:
        default_amount = fixed_bill['amount']
        if default_amount > 0:
            ratio = amount / default_amount
            
            if ratio > 2:
                response += f"📊 Usually {fmt(default_amount)} - this is {ratio:.0f}x higher!\n"
                if 'gas' in category.lower():
                    response += "🔥 Winter heating? (Default unchanged)"
                elif 'electric' in category.lower():
                    response += "❄️ AC or heating? (Default unchanged)"
                else:
                    response += "(Default unchanged)"
            elif ratio > 1.2:
                response += f"📊 {fmt(amount - default_amount)} more than usual ({fmt(default_amount)})"
            elif ratio < 0.5:
                response += f"📊 Usually {fmt(default_amount)} - nice savings! 🎉"
            elif ratio < 0.8:
                response += f"📊 {fmt(default_amount - amount)} less than usual ({fmt(default_amount)})"
    
    return response

# Build response for regular transaction
def build_transaction_response(tx_data):
    is_backdated = tx_data.get('is_backdated', False)
    year = tx_data.get('year')
    month = tx_data.get('month')
    
    response = f"✅ Logged: {tx_data['type']} - {tx_data['person']} - {fmt(tx_data['amount'])} - {tx_data['description']}"
    
    if is_backdated:
        month_name = f"{MONTH_NAMES_REVERSE[month]} {year}"
        response += f"\n📅 {month_name} (backdated)"
    
    return response

# Get fund status
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
    seen = set()
    total = 0
    for key, b in bills.items():
        cat = b['category']
        if cat not in seen:
            seen.add(cat)
            total += b['amount']
    return total

# Slack events handler
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
        
        try:
            user_info = slack_client.users_info(user=user_id)
            user_name = user_info['user']['real_name'].split()[0]
            if 'naomi' in user_name.lower() or 'nao' in user_name.lower():
                user_name = 'Naomi'
            else:
                user_name = 'Jacob'
        except:
            user_name = 'Jacob'
        
        text_lower = text.lower()
        
        # Command: status
        if text_lower in ['status', 'tình hình', 'báo cáo', 'check']:
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
                    
                    emergency = funds.get('Emergency Fund', {}).get('amount', 0)
                    if emergency:
                        progress = (emergency / 15000000) * 100
                        msg += f"\n🎯 Emergency Fund: {progress:.1f}% → ₩15M"
                
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text="❌ Cannot fetch status")
        
        # Command: bills
        elif text_lower in ['bills', 'fixed', 'fixed bills']:
            bills = get_fixed_bills_dict()
            msg = "📋 *Fixed Bills (Active):*\n\n"
            
            jacob_bills, naomi_bills, joint_bills = [], [], []
            seen = set()
            total = 0
            
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
        
        # Command: list
        elif text_lower.startswith('list') or text_lower.startswith('last'):
            # Handle 'last 5' as 'list' with limit
            if text_lower.startswith('last'):
                words = text_lower.split()
                limit = int(words[1]) if len(words) > 1 and words[1].isdigit() else 5
                filter_type, filter_category, filter_person, filter_month, _ = None, None, None, None, limit
            else:
                filter_type, filter_category, filter_person, filter_month, limit = parse_list_command(text_lower)
            
            transactions = get_all_transactions()
            filtered = filter_transactions(transactions, filter_type, filter_category, filter_person, filter_month, limit)
            
            # Build title
            title_parts = []
            if filter_category:
                title_parts.append(filter_category.title())
            if filter_type:
                title_parts.append(filter_type)
            if filter_person:
                title_parts.append(filter_person)
            if filter_month:
                try:
                    month_obj = datetime.strptime(filter_month, '%Y-%m')
                    title_parts.append(month_obj.strftime('%B %Y'))
                except:
                    title_parts.append(filter_month)
            if limit:
                title_parts.append(f"Last {limit}")
            
            title = ' - '.join(title_parts) if title_parts else 'All Transactions'
            
            msg = format_transaction_list(filtered, title, channel)
            slack_client.chat_postMessage(channel=channel, text=msg)
        
        # Command: delete
        elif text_lower.startswith('delete'):
            words = text_lower.split()
            
            if len(words) < 2:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `delete 1` or `delete last`")
                return jsonify({'ok': True})
            
            target = words[1]
            
            # Get the transaction to delete
            if target == 'last':
                transactions = get_all_transactions()
                if not transactions:
                    slack_client.chat_postMessage(channel=channel, text="❌ No transactions to delete")
                    return jsonify({'ok': True})
                tx_to_delete = sorted(transactions, key=lambda x: x['date'], reverse=True)[0]
            elif target.isdigit():
                idx = int(target) - 1
                if channel not in last_list_results or idx >= len(last_list_results[channel]):
                    slack_client.chat_postMessage(channel=channel, text="❌ Invalid number. Use `list` first, then `delete 1`")
                    return jsonify({'ok': True})
                tx_to_delete = last_list_results[channel][idx]
            else:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `delete 1` or `delete last`")
                return jsonify({'ok': True})
            
            success, result = delete_transaction(tx_to_delete['row_index'], channel)
            
            if success:
                msg = f"🗑️ Deleted: {tx_to_delete['category']} - {fmt(tx_to_delete['amount'])} - {tx_to_delete['date'][:10]}\n"
                msg += "↩️ To undo: `undo`"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ Error: {result}")
        
        # Command: edit
        elif text_lower.startswith('edit'):
            words = text.split()
            
            if len(words) < 3:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `edit 1 150K` (edit item #1 to ₩150K)")
                return jsonify({'ok': True})
            
            target = words[1]
            new_amount_str = words[2]
            
            if not target.isdigit():
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `edit 1 150K`")
                return jsonify({'ok': True})
            
            idx = int(target) - 1
            if channel not in last_list_results or idx >= len(last_list_results[channel]):
                slack_client.chat_postMessage(channel=channel, text="❌ Invalid number. Use `list` first, then `edit 1 150K`")
                return jsonify({'ok': True})
            
            tx_to_edit = last_list_results[channel][idx]
            new_amount = parse_amount(new_amount_str)
            
            success, old_value = edit_transaction(tx_to_edit['row_index'], new_amount)
            
            if success:
                msg = f"✏️ Updated: {tx_to_edit['category']}\n"
                msg += f"   {fmt(int(float(old_value)))} → {fmt(new_amount)}"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ Error: {old_value}")
        
        # Command: undo
        elif text_lower == 'undo':
            success, result = undo_delete(channel)
            
            if success:
                msg = f"↩️ Restored: {result[2]} - {fmt(int(float(result[3])))}"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ {result}")
        
        # Command: help
        elif text_lower in ['help', 'trợ giúp', '?']:
            help_msg = """🤖 *Finance Bot v4 Commands:*

*➕ Add Transaction:*
• `jacob 2.8M salary` - Log income
• `gas 150K` - Log expense (smart comparison)
• `gas dec 119910` - Log for past month

*📋 List Transactions:*
• `list` - This month's transactions
• `list dec` - December transactions
• `list gas` - All gas bills
• `list gas 5` - Last 5 gas bills
• `list expense` - This month's expenses
• `last 5` - Last 5 transactions

*✏️ Edit & Delete:*
• `delete 1` - Delete item #1 from list
• `delete last` - Delete most recent
• `edit 1 150K` - Change amount of item #1
• `undo` - Undo last delete

*📊 Status:*
• `status` - Monthly summary + funds
• `bills` - View fixed bills"""
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
                        response = build_transaction_response(tx)
                else:
                    response = f"❌ Error: {msg}"
                slack_client.chat_postMessage(channel=channel, text=response)
    
    return jsonify({'ok': True})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Couple Finance Bot v4'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

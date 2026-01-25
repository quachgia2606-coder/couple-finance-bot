import os
import re
import json
import random
import unicodedata
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

# ============== USER ID MAPPING ==============
# Direct mapping of Slack User IDs to names - most reliable method
NAOMI_USER_IDS = ['U0AAFBUSNSD']

def detect_user_name(user_id):
    """Detect user by Slack User ID"""
    if user_id in NAOMI_USER_IDS:
        return 'Naomi'
    return 'Jacob'

# ============== DUPLICATE EVENT PREVENTION ==============
processed_events = set()
MAX_PROCESSED_EVENTS = 100

def is_duplicate_event(event_id):
    global processed_events
    
    if not event_id:
        return False
    
    if event_id in processed_events:
        return True
    
    processed_events.add(event_id)
    
    if len(processed_events) > MAX_PROCESSED_EVENTS:
        processed_events = set(list(processed_events)[-50:])
    
    return False

# ============== UNIVERSAL UNDO SYSTEM ==============
# Stores last action for each channel for undo
last_action = {}

def store_undo_action(channel_id, action_type, data):
    """Store action for potential undo"""
    last_action[channel_id] = {
        'type': action_type,  # 'delete', 'add', 'edit', 'paid'
        'data': data,
        'timestamp': datetime.now()
    }

def get_undo_action(channel_id):
    """Get stored action if within time limit"""
    if channel_id not in last_action:
        return None, "Nothing to undo (bot may have restarted)"
    
    action = last_action[channel_id]
    time_diff = (datetime.now() - action['timestamp']).seconds
    
    if time_diff > 600:  # 10 minutes
        return None, "Undo expired (>10 minutes)"
    
    return action, None

def clear_undo_action(channel_id):
    """Clear undo action after use"""
    if channel_id in last_action:
        del last_action[channel_id]

# ============== STORAGE FOR LIST RESULTS ==============
last_list_results = {}
last_debt_list = {}

# ============== MASTER CATEGORIES ==============
CATEGORIES = {
    'Food & Dining': {
        'keywords': ['eat', 'dinner', 'lunch', 'breakfast', 'restaurant', 'coffee', 'cafe', 'meal', 'food',
                     'ăn', 'cơm', 'phở', 'bún', 'bánh mì', 'cà phê', 'cafe', 'nhà hàng', 'ăn trưa', 'ăn tối', 
                     'ăn sáng', 'quán', 'gọi đồ ăn', 'delivery', 'đặt đồ ăn', 'ăn vặt', 'trà sữa', 'kem', 
                     'lẩu', 'nướng', 'bbq', 'thịt nướng', 'samgyupsal', 'chimaek', 'chicken', 'gà rán',
                     'bún bò', 'bún chả', 'bánh cuốn', 'chè', 'snack', 'đồ ăn'],
        'emoji': ['🍜', '☕', '🍕', '🍔', '🍱'],
        'responses': ["Yummy! 😋", "맛있게 드세요!", "Ăn ngon nha!", "Enjoy your meal! 🍴", "Tasty! 😄"]
    },
    'Groceries': {
        'keywords': ['grocery', 'groceries', 'market', 'supermarket', 'mart',
                     'đi chợ', 'siêu thị', 'thực phẩm', 'coupang', '쿠팡', 'emart', 'homeplus', 
                     'lotte mart', 'rau', 'thịt', 'trứng', 'sữa', 'gạo', 'chợ'],
        'emoji': ['🛒', '🥬', '🥚'],
        'responses': ["Stocking up! 🛒", "Coupang delivery? 📦", "Fresh groceries! 🥬"]
    },
    'Transport': {
        'keywords': ['grab', 'taxi', 'bus', 'subway', 'train', 'ktx', 'parking', 'toll',
                     'xe', '택시', 'xe buýt', 'tàu điện', '지하철', 'gửi xe', 'đỗ xe', 
                     'phí cầu đường', 'xăng', 'đổ xăng', 'uber', 'kakao taxi', 'đi lại'],
        'emoji': ['🚕', '🚇', '🚗'],
        'responses': ["Safe travels! 🚗", "Đi cẩn thận nha!", "On the move! 🚇"]
    },
    'Gift': {
        'keywords': ['gift', 'present', 'wedding gift', 'birthday', 'baby shower',
                     'quà', 'tặng', 'quà cưới', 'mừng cưới', 'quà sinh nhật', 'sinh nhật',
                     'đám cưới', '돌잔치', 'thôi nôi', 'quà tân gia', 'tặng bạn', 'mừng'],
        'emoji': ['🎁', '💝', '🎀'],
        'responses': ["So thoughtful! 💕", "Người nhận sẽ vui lắm!", "Nice gift! 🎁", "Generous! 💝"]
    },
    'Tithe & Offering': {
        'keywords': ['tithe', 'offering', 'dâng hiến', 'dâng 1/10', 'tiền dâng', 'hiến tế',
                     'nhà thờ', 'church', 'đóng góp nhà thờ'],
        'emoji': ['⛪', '🙏', '✝️'],
        'responses': ["God bless! ⛪", "Dâng hiến cho Chúa! 🙏", "Blessed giving! ✝️"]
    },
    'Family Support': {
        'keywords': ['mom', 'dad', 'parents', 'family', 'send home',
                     'cho mẹ', 'cho ba', 'biếu', 'hỗ trợ gia đình', 'gửi về', 'gửi tiền',
                     'tiền nhà', 'bố mẹ', 'gia đình', 'cho bố', 'mẹ', 'ba', 'bố'],
        'emoji': ['👨‍👩‍👧', '❤️', '🏠'],
        'responses': ["Family first! ❤️", "Hiếu thảo quá! 👏", "Family love! 👨‍👩‍👧"]
    },
    'Date': {
        'keywords': ['date', 'dating', 'couple', 'anniversary', 'romantic', 'valentine',
                     'hẹn hò', 'kỷ niệm', 'lãng mạn', 'đi chơi hai đứa', 'tình yêu'],
        'emoji': ['💑', '🥰', '💕'],
        'responses': ["Enjoy your date! 💕", "Have fun you two! 🥰", "Love is in the air! 💑"]
    },
    'Entertainment': {
        'keywords': ['movie', 'game', 'netflix', 'concert', 'karaoke', 'pc bang',
                     'phim', 'xem phim', 'giải trí', 'game', '노래방', 'pc방', 'youtube', 'spotify'],
        'emoji': ['🎬', '🎮', '🎤'],
        'responses': ["Have fun! 🎉", "Giải trí xíu! 🎬", "Enjoy! 🎮"]
    },
    'Shopping': {
        'keywords': ['buy', 'purchase', 'clothes', 'shoes', 'daiso', 'olive young', 'shop',
                     'mua', 'quần áo', 'giày dép', 'shopping', 'mỹ phẩm', 'skincare', 
                     '다이소', '올리브영', 'mua sắm', 'đồ', 'áo', 'quần'],
        'emoji': ['🛍️', '👗', '👟'],
        'responses': ["Treat yourself! 🛍️", "Shopping therapy! 💅", "Nice buy! 👍"]
    },
    'Travel': {
        'keywords': ['flight', 'ticket', 'hotel', 'travel', 'trip', 'airbnb', 'booking',
                     'vé máy bay', 'vé', 'khách sạn', 'du lịch', 'về việt nam', 'về quê', 
                     'bay', 'book', 'đặt phòng', 'resort', 'nghỉ dưỡng'],
        'emoji': ['✈️', '🧳', '🏖️'],
        'responses': ["Bon voyage! ✈️", "Safe travels!", "Du lịch vui nha! 🌴", "Về quê! 🇻🇳❤️"]
    },
    'Healthcare': {
        'keywords': ['doctor', 'hospital', 'medicine', 'pharmacy', 'clinic', 'health',
                     'bác sĩ', 'thuốc', 'bệnh viện', '병원', '약국', 'khám bệnh', 'hiệu thuốc',
                     'vitamin', 'sick', 'ốm', 'bệnh'],
        'emoji': ['💊', '🏥', '💪'],
        'responses': ["Health is wealth! 💪", "Get well soon!", "Take care! 🏥"]
    },
    'Loan & Debt': {
        'keywords': ['lend', 'borrow', 'debt', 'loan', 'repay', 'pay back',
                     'cho mượn', 'mượn', 'trả nợ', 'vay', 'nợ', 'trả lại', 'cho vay',
                     'thiếu', 'lending', 'owed', 'trả tiền'],
        'emoji': ['🤝', '💸', '📝'],
        'responses': ["Loan tracked! 🤝", "Don't forget to follow up! 📝", "Noted! 💸"]
    },
    'Business': {
        'keywords': ['ads', 'contractor', 'client', 'marketing', 'revenue', 'business',
                     'quảng cáo', 'cộng tác viên', 'khách hàng', 'doanh thu', 'công việc',
                     'ad spend', 'facebook ads', 'campaign', 'tiền quảng cáo', 'chi phí quảng cáo',
                     'phí quảng cáo', 'gởi chị dương', 'tiền chị dương', 'chị dương'],
        'emoji': ['💼', '📈', '💹'],
        'responses': ["Business expense logged! 💼", "Invest to grow! 📈", "Business moves! 💹"]
    },
    'Subscription': {
        'keywords': ['subscription', 'monthly', 'netflix', 'spotify', 'claude', 'chatgpt',
                     'đăng ký', 'gói tháng', 'youtube premium', 'disney', 'apple'],
        'emoji': ['📱', '💳', '🔄'],
        'responses': ["Subscription noted! 📱", "Monthly fee logged! 💳"]
    },
    'Housing': {
        'keywords': ['rent', 'deposit', 'maintenance', '관리비', '월세', 'apartment',
                     'tiền nhà', 'thuê nhà', 'đặt cọc', 'bảo trì', 'nhà', 'phòng'],
        'emoji': ['🏠', '🔑', '🏢'],
        'responses': ["Home sweet home! 🏠", "Housing cost noted! 🔑"]
    },
    'Education': {
        'keywords': ['course', 'class', 'book', 'study', 'korean class', 'learn', 'school',
                     'học', 'khóa học', 'lớp', 'sách', 'học tiếng hàn', '한국어', 'tiếng hàn'],
        'emoji': ['📚', '🎓', '✏️'],
        'responses': ["Invest in yourself! 📚", "Knowledge is power! 🎓", "Keep learning! ✏️"]
    },
    'Pet': {
        'keywords': ['pet', 'cat', 'dog', 'vet', 'mèo', 'chó', 'thú cưng', 'thú y', 'pet food'],
        'emoji': ['🐱', '🐕', '🐾'],
        'responses': ["For the fur baby! 🐾", "Pet parent life! 🐱"]
    },
    'Income': {
        'keywords': ['salary', 'commission', 'bonus', 'income', 'revenue', 'wage', 'pay',
                     'lương', 'hoa hồng', 'thưởng', 'thu nhập', 'tiền lương'],
        'emoji': ['💰', '🎉', '💵'],
        'responses': ["Money in! 💰", "Cha-ching! 🎉", "Nice! Keep it coming! 💪", "Pay day! 💵"]
    },
    'Emergency Fund': {
        'keywords': ['emergency fund', 'quỹ khẩn cấp', 'quy khan cap'],
        'emoji': ['🎯', '💰', '🚨'],
        'responses': ["Building your safety net! 🎯", "Emergency fund growing! 💪", "Smart saving! 🚨"]
    },
    'Investment Fund': {
        'keywords': ['investment fund', 'quỹ đầu tư', 'quy dau tu'],
        'emoji': ['📈', '💹', '💰'],
        'responses': ["Investing in your future! 📈", "Growing your wealth! 💹", "Smart investing! 💰"]
    },
    'Planning Fund': {
        'keywords': ['planning fund', 'quỹ kế hoạch', 'quy ke hoach'],
        'emoji': ['🏠', '📋', '🎯'],
        'responses': ["Planning ahead! 🏠", "Future goals! 📋", "Building your plans! 🎯"]
    },
    'Date Fund': {
        'keywords': ['date fund', 'quỹ hẹn hò', 'quy hen ho'],
        'emoji': ['💕', '💑', '🥰'],
        'responses': ["Date fund growing! 💕", "Love & memories! 💑", "Quality time fund! 🥰"]
    },
}

# Income keywords - EXACT word match to avoid "coffee" containing "fee"
INCOME_KEYWORDS_EXACT = ['salary', 'commission', 'bonus', 'income', 'revenue', 'wage', 'pay',
                         'lương', 'hoa hồng', 'thưởng', 'thu nhập', 'tiền lương', 'fee']

# Loan keywords for detection
LOAN_KEYWORDS = ['cho mượn', 'mượn', 'cho vay', 'vay', 'nợ', 'thiếu', 'lend', 'borrow', 'loan', 'debt', 'owed']
REPAY_KEYWORDS = ['trả nợ', 'trả lại', 'repay', 'pay back', 'paid back', 'trả tiền', 'nhận lại']

MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
    'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
    'thg1': 1, 'thg2': 2, 'thg3': 3, 'thg4': 4, 'thg5': 5, 'thg6': 6,
    'thg7': 7, 'thg8': 8, 'thg9': 9, 'thg10': 10, 'thg11': 11, 'thg12': 12,
}

MONTH_NAMES_REVERSE = {
    1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
    7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
}

# ============== HELPER FUNCTIONS ==============

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

def parse_amount(amount_str):
    amount_str = str(amount_str).replace(',', '').replace('₩', '').replace(' ', '').strip()
    match = re.match(r'^([\d.]+)([mkMK]?)$', amount_str)
    if match:
        num = float(match.group(1))
        suffix = match.group(2).upper()
        if suffix == 'M':
            return int(num * 1000000)
        elif suffix == 'K':
            return int(num * 1000)
        return int(num)
    return None

def fmt(amount):
    if amount >= 1000000:
        return f"₩{amount/1000000:.1f}M"
    elif amount >= 1000:
        return f"₩{amount/1000:.0f}K"
    return f"₩{amount:,.0f}"

def extract_amount_from_text(text):
    words = text.split()
    amount = None
    remaining_words = []
    
    for word in words:
        parsed = parse_amount(word)
        if parsed and amount is None:
            amount = parsed
        else:
            remaining_words.append(word)
    
    return amount, ' '.join(remaining_words)

def parse_month(text):
    text = text.lower().strip()
    now = datetime.now()
    
    match = re.match(r'^(\d{4})-(\d{1,2})$', text)
    if match:
        return int(match.group(1)), int(match.group(2))
    
    if text in MONTH_NAMES:
        month = MONTH_NAMES[text]
        year = now.year if month <= now.month else now.year - 1
        return year, month
    
    return None

def extract_month_from_text(text):
    words = text.split()
    now = datetime.now()
    
    for i, word in enumerate(words):
        month_info = parse_month(word.lower())
        if month_info:
            year, month = month_info
            cleaned_words = words[:i] + words[i+1:]
            cleaned_text = ' '.join(cleaned_words)
            is_backdated = not (year == now.year and month == now.month)
            return cleaned_text, year, month, is_backdated
    
    return text, now.year, now.month, False

def extract_person_from_text(text):
    words = text.lower().split()
    person = None
    remaining_words = []
    
    for word in words:
        if word in ['jacob', 'naomi', 'joint']:
            person = word.capitalize()
        else:
            remaining_words.append(word)
    
    return person, ' '.join(remaining_words)

def detect_category(text):
    text_lower = text.lower()
    
    for category, data in CATEGORIES.items():
        for keyword in data['keywords']:
            if keyword in text_lower:
                return category, data
    
    return 'Other', {'emoji': ['📝'], 'responses': ["Logged! 📝"]}

def is_income(text, category):
    """Check if transaction is income - using EXACT word match"""
    text_lower = text.lower()
    
    if category == 'Income':
        return True
    
    words = re.findall(r'\b\w+\b', text_lower)
    
    for keyword in INCOME_KEYWORDS_EXACT:
        if keyword in words:
            return True
    
    return False

def is_loan_transaction(text):
    """Check if transaction is a loan/debt"""
    text_lower = text.lower()
    for keyword in LOAN_KEYWORDS:
        if keyword in text_lower:
            return True
    return False

def is_repayment(text):
    """Check if transaction is a repayment"""
    text_lower = text.lower()
    for keyword in REPAY_KEYWORDS:
        if keyword in text_lower:
            return True
    return False

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
            
            bill_data = {
                'category': category,
                'amount': row.get('Amount', 0),
                'type': row.get('Type', 'Personal'),
                'person': row.get('Person', 'Joint'),
            }
            bills[key] = bill_data
            if simple_key != key:
                bills[simple_key] = bill_data
    
    return bills

def find_fixed_bill(text):
    bills = get_fixed_bills_dict()
    text_lower = text.lower().strip()
    
    if text_lower in bills:
        return bills[text_lower]
    
    for key, bill in bills.items():
        if text_lower in key or key in text_lower:
            return bill
    
    aliases = {
        'gas': 'gas', 'electricity': 'electricity', 'electric': 'electricity',
        'internet': 'internet', 'wifi': 'internet', 'rent': 'rent',
        'điện': 'electricity', 'nước': 'water', 'mạng': 'internet',
    }
    
    if text_lower in aliases:
        alias_key = aliases[text_lower]
        for key, bill in bills.items():
            if alias_key in key:
                return bill
    
    return None

def get_personality_response(category, category_data, amount, is_income_tx):
    if random.random() > 0.5:
        return ""
    
    responses = category_data.get('responses', ["Logged! 📝"])
    
    if is_income_tx and amount >= 5000000:
        return random.choice(["🎊 WOW! Amazing! 🚀", "Big income! 💰💰💰", "Incredible! Keep it up! 🔥"])
    
    if not is_income_tx and amount >= 1000000:
        return random.choice(["Big purchase! 🛒", "That's a big one! 💸"])
    
    return random.choice(responses)

def get_emoji(category, category_data, is_income_tx):
    if is_income_tx:
        return random.choice(['💰', '💵', '🎉'])
    return random.choice(category_data.get('emoji', ['📝']))

# ============== DUPLICATE INCOME CHECK ==============

def check_duplicate_income(tx_data):
    if tx_data['type'] != 'Income':
        return None
    
    sheet = get_transaction_sheet()
    if not sheet:
        return None
    
    records = sheet.get_all_records()
    today = datetime.now().strftime('%Y-%m-%d')
    amount = tx_data['amount']
    description_lower = tx_data['description'].lower()
    
    for row in records:
        if (row.get('Type') == 'Income' and 
            row.get('Date') == today and
            row.get('Amount') == amount):
            row_desc = str(row.get('Description', '')).lower()
            if (description_lower in row_desc or 
                row_desc in description_lower or
                'lương' in description_lower and 'lương' in row_desc or
                'salary' in description_lower and 'salary' in row_desc or
                'commission' in description_lower and 'commission' in row_desc):
                return row
    
    return None

# ============== LOAN/DEBT FUNCTIONS ==============

def get_outstanding_loans():
    """Get all loan/debt transactions that haven't been marked as paid"""
    sheet = get_transaction_sheet()
    if not sheet:
        return []
    
    records = sheet.get_all_records()
    loans = []
    
    for i, row in enumerate(records):
        if row.get('Type') == 'Expense' and row.get('Category') == 'Loan & Debt':
            description = str(row.get('Description', ''))
            # Skip if it's marked as [PAID] or is a repayment
            if not description.startswith('[PAID]') and not is_repayment(description.lower()):
                loans.append({
                    'row_index': i + 2,
                    'date': row.get('Date', ''),
                    'type': 'Expense',
                    'category': 'Loan & Debt',
                    'amount': row.get('Amount', 0),
                    'description': description,
                    'person': row.get('Person', ''),
                    'month': row.get('Month', ''),
                })
    
    return loans

def has_outstanding_loans():
    """Check if there are any outstanding loans"""
    loans = get_outstanding_loans()
    return len(loans) > 0

def mark_loan_as_paid(loan_index, channel_id):
    """Mark a loan as paid by adding [PAID] prefix and logging income"""
    if channel_id not in last_debt_list or loan_index >= len(last_debt_list[channel_id]):
        return False, "Invalid loan number. Use `list debt` first.", None
    
    loan = last_debt_list[channel_id][loan_index]
    
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", None
    
    # 1. Update original loan description with [PAID] prefix
    try:
        original_desc = loan['description']
        new_desc = f"[PAID] {original_desc}"
        sheet.update_cell(loan['row_index'], 5, new_desc)  # Column 5 = Description
    except Exception as e:
        return False, f"Error updating loan: {e}", None
    
    # 2. Log income entry for repayment
    now = datetime.now()
    income_row = [
        now.strftime('%Y-%m-%d'),
        'Income',
        'Loan & Debt',
        loan['amount'],
        f"nhận lại/trả nợ: {original_desc}",
        loan['person'],
        now.strftime('%Y-%m-01'),
        'slack'
    ]
    
    sheet.append_row(income_row)
    
    # Store for undo
    return True, loan, {
        'loan_row_index': loan['row_index'],
        'original_desc': original_desc,
        'income_row_data': income_row
    }

def undo_paid(undo_data):
    """Undo a paid action - remove [PAID] prefix and delete income entry
    Supports both single undo_data (dict) and multiple (list of dicts)"""
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"

    try:
        # Handle both single and multiple payments
        undo_list = undo_data if isinstance(undo_data, list) else [undo_data]

        for data in undo_list:
            # 1. Restore original description (remove [PAID] prefix)
            sheet.update_cell(data['loan_row_index'], 5, data['original_desc'])

            # 2. Find and delete the income entry
            records = sheet.get_all_records()
            for i, row in enumerate(records):
                if (row.get('Type') == 'Income' and
                    row.get('Category') == 'Loan & Debt' and
                    f"nhận lại/trả nợ: {data['original_desc']}" in str(row.get('Description', ''))):
                    sheet.delete_rows(i + 2)
                    break

        count = len(undo_list)
        msg = f"Paid action undone" if count == 1 else f"{count} paid actions undone"
        return True, msg
    except Exception as e:
        return False, str(e)

# ============== TRANSACTION PARSING ==============

def parse_transaction(text, user_name):
    original_text = text.strip()
    
    text, year, month, is_backdated = extract_month_from_text(original_text)
    person, text = extract_person_from_text(text)
    if not person:
        person = user_name

    # Check for joint expense keywords
    is_joint = False
    joint_keywords = ['joint', 'quỹ chung', 'chung']
    text_lower = text.lower()
    for keyword in joint_keywords:
        if keyword in text_lower:
            is_joint = True
            person = 'Joint'
            # Remove joint keyword from text
            text = text.replace(keyword, '').replace(keyword.capitalize(), '').replace(keyword.upper(), '')
            text = ' '.join(text.split())  # Clean up extra spaces
            break

    amount, description = extract_amount_from_text(text)
    
    if not amount:
        return None
    
    description = description.strip()
    if not description:
        description = "Transaction"
    
    fixed_bill = find_fixed_bill(description)
    
    if fixed_bill:
        category = fixed_bill['category']
        category_data = {'emoji': ['📋'], 'responses': ["Fixed bill logged! 📋"]}
        if fixed_bill['person'] != 'Both':
            person = fixed_bill['person']
        else:
            person = 'Joint'
    else:
        if is_loan_transaction(description):
            category = 'Loan & Debt'
            category_data = CATEGORIES.get('Loan & Debt', {'emoji': ['🤝'], 'responses': ["Loan tracked! 🤝"]})
        else:
            category, category_data = detect_category(description)
    
    tx_is_income = is_income(description, category)
    
    if is_repayment(description):
        tx_is_income = True
        category = 'Loan & Debt'
    
    return {
        'person': person,
        'amount': amount,
        'description': description,
        'category': category,
        'category_data': category_data,
        'type': 'Income' if tx_is_income else 'Expense',
        'fixed_bill': fixed_bill,
        'year': year,
        'month': month,
        'is_backdated': is_backdated,
        'is_loan': is_loan_transaction(description),
        'is_joint': is_joint
    }

# ============== TRANSACTION LOGGING ==============

def log_transaction(tx_data):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", None
    
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
        tx_data['category'],
        tx_data['amount'],
        tx_data['description'],
        tx_data['person'],
        month_start,
        'slack'
    ]
    
    sheet.append_row(row)
    
    # Get the row index of newly added row
    all_values = sheet.get_all_values()
    new_row_index = len(all_values)
    
    return True, "Transaction logged!", {'row_index': new_row_index, 'row_data': row}

def delete_row_by_index(row_index):
    """Delete a row by index"""
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        row_data = sheet.row_values(row_index)
        sheet.delete_rows(row_index)
        return True, row_data
    except Exception as e:
        return False, str(e)

def build_response(tx_data, duplicate_warning=None):
    category = tx_data['category']
    category_data = tx_data.get('category_data', {})
    amount = tx_data['amount']
    description = tx_data['description']
    is_income_tx = tx_data['type'] == 'Income'
    is_backdated = tx_data.get('is_backdated', False)
    year = tx_data.get('year')
    month = tx_data.get('month')
    fixed_bill = tx_data.get('fixed_bill')
    is_loan = tx_data.get('is_loan', False)
    is_joint = tx_data.get('is_joint', False)

    emoji = get_emoji(category, category_data, is_income_tx)

    response = f"{emoji} Logged: {category} - {fmt(amount)}\n"
    response += f"📝 {description}\n"

    if is_joint:
        response += f"👥 Joint Expense\n"

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
                    response += "🔥 Winter heating?"
                elif 'electric' in category.lower():
                    response += "❄️ AC or heating?"
            elif ratio > 1.2:
                response += f"📊 {fmt(amount - default_amount)} more than usual"
            elif ratio < 0.5:
                response += f"📊 Usually {fmt(default_amount)} - nice savings! 🎉"
    
    if is_loan:
        response += "\n💡 Track with `list debt`"
    
    if duplicate_warning:
        response += f"\n\n⚠️ *Warning:* You already logged {fmt(amount)} \"{description}\" today!"
        response += "\nDuplicate? Use `undo` to remove."
    else:
        personality = get_personality_response(category, category_data, amount, is_income_tx)
        if personality:
            response += f"\n{personality}"
    
    return response

# ============== LIST/DELETE/EDIT FUNCTIONS ==============

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
                'row_index': i + 2,
                'date': row.get('Date', ''),
                'type': tx_type,
                'category': row.get('Category', ''),
                'amount': row.get('Amount', 0),
                'description': row.get('Description', ''),
                'person': row.get('Person', ''),
                'month': row.get('Month', ''),
            })
    
    return transactions

def filter_transactions(transactions, filter_type=None, filter_category=None, filter_person=None, filter_month=None, limit=None):
    filtered = transactions
    
    if filter_type:
        filtered = [t for t in filtered if t['type'].lower() == filter_type.lower()]
    
    if filter_category:
        if filter_category.lower() in ['debt', 'loan', 'nợ', 'mượn']:
            filtered = [t for t in filtered if t['category'] == 'Loan & Debt']
        else:
            filtered = [t for t in filtered if filter_category.lower() in t['category'].lower() or 
                        filter_category.lower() in t['description'].lower()]
    
    if filter_person:
        filtered = [t for t in filtered if t['person'].lower() == filter_person.lower()]
    
    if filter_month:
        filtered = [t for t in filtered if t['month'][:7] == filter_month]
    
    filtered = sorted(filtered, key=lambda x: x['date'], reverse=True)
    
    if limit:
        filtered = filtered[:limit]
    
    return filtered

def parse_list_command(text):
    words = text.lower().split()[1:]
    
    filter_type = None
    filter_category = None
    filter_person = None
    filter_month = None
    limit = None
    
    now = datetime.now()
    
    for word in words:
        if word.isdigit():
            limit = int(word)
        elif word in MONTH_NAMES:
            month_num = MONTH_NAMES[word]
            year = now.year if month_num <= now.month else now.year - 1
            filter_month = f"{year}-{month_num:02d}"
        elif word in ['income', 'expense']:
            filter_type = word.capitalize()
        elif word in ['jacob', 'naomi', 'joint']:
            filter_person = word.capitalize()
        else:
            filter_category = word
    
    if not filter_month and not filter_category and not limit:
        filter_month = now.strftime('%Y-%m')
    
    return filter_type, filter_category, filter_person, filter_month, limit

def format_transaction_list(transactions, title, channel_id, is_debt_list=False):
    if not transactions:
        return "📋 No transactions found."
    
    last_list_results[channel_id] = transactions
    if is_debt_list:
        last_debt_list[channel_id] = transactions
    
    msg = f"📋 *{title}:*\n\n"
    
    for i, tx in enumerate(transactions[:20], 1):
        date_str = tx['date'][:10]
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            date_display = date_obj.strftime('%b %d')
        except:
            date_display = date_str
        
        emoji = "💵" if tx['type'] == 'Income' else "💸"
        amount = tx['amount'] or 0
        description = tx['description'][:30] + "..." if len(tx['description']) > 30 else tx['description']
        
        msg += f"{i}. {emoji} {date_display} | {tx['category']} | {fmt(amount)} | {description}\n"
    
    if len(transactions) > 20:
        msg += f"\n... and {len(transactions) - 20} more"
    
    if is_debt_list:
        msg += f"\n\n*Mark as paid:* `paid 1`"
    else:
        msg += f"\n\n*Delete:* `delete 1` or `delete 1,2,3`"
    
    return msg

def parse_delete_targets(target_str):
    targets = []
    
    if target_str.startswith('last'):
        parts = target_str.split()
        if len(parts) > 1 and parts[1].isdigit():
            return ['last', int(parts[1])]
        return ['last']
    
    parts = target_str.replace(' ', '').split(',')
    
    for part in parts:
        if '-' in part and not part.startswith('-'):
            range_parts = part.split('-')
            if len(range_parts) == 2 and range_parts[0].isdigit() and range_parts[1].isdigit():
                start = int(range_parts[0])
                end = int(range_parts[1])
                targets.extend(range(start, end + 1))
        elif part.isdigit():
            targets.append(int(part))
    
    return sorted(list(set(targets)), reverse=True)

def delete_transactions(targets, channel_id):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", []
    
    transactions = get_all_transactions()
    
    if targets and targets[0] == 'last':
        count = targets[1] if len(targets) > 1 else 1
        sorted_tx = sorted(transactions, key=lambda x: x['date'], reverse=True)
        targets = [i + 1 for i in range(min(count, len(sorted_tx)))]
        last_list_results[channel_id] = sorted_tx
    
    if channel_id not in last_list_results:
        return False, "Use `list` first to see transactions", []
    
    list_results = last_list_results[channel_id]
    deleted_items = []
    deleted_rows_data = []
    
    for idx in targets:
        if idx < 1 or idx > len(list_results):
            return False, f"Invalid number: {idx}. Use `list` first.", []
    
    sorted_targets = sorted(targets, reverse=True)
    
    try:
        for idx in sorted_targets:
            tx = list_results[idx - 1]
            row_data = sheet.row_values(tx['row_index'])
            deleted_rows_data.append({
                'row_data': row_data,
                'tx': tx
            })
            sheet.delete_rows(tx['row_index'])
            deleted_items.append(tx)
            
            for item in list_results:
                if item['row_index'] > tx['row_index']:
                    item['row_index'] -= 1
        
        return True, "Deleted successfully", deleted_items, deleted_rows_data
    
    except Exception as e:
        return False, str(e), [], []

def undo_delete(deleted_rows_data):
    """Undo delete by restoring rows"""
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        restored = []
        for item in deleted_rows_data:
            sheet.append_row(item['row_data'])
            restored.append(item['tx'])
        return True, restored
    except Exception as e:
        return False, str(e)

def edit_transaction(row_index, new_amount):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", None
    
    try:
        old_value = sheet.cell(row_index, 4).value
        sheet.update_cell(row_index, 4, new_amount)
        return True, old_value, {'row_index': row_index, 'old_amount': int(float(old_value)), 'new_amount': new_amount}
    except Exception as e:
        return False, str(e), None

def undo_edit(edit_data):
    """Undo edit by restoring old amount"""
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        sheet.update_cell(edit_data['row_index'], 4, edit_data['old_amount'])
        return True, f"Restored amount to {fmt(edit_data['old_amount'])}"
    except Exception as e:
        return False, str(e)

# ============== STATUS FUNCTIONS ==============

def get_fund_status():
    sheet = get_transaction_sheet()
    if not sheet:
        return None

    records = sheet.get_all_records()
    funds = {}

    for row in records:
        row_type = row.get('Type', '')
        fund_name = row.get('Category', '')
        amount = row.get('Amount', 0) or 0

        if row_type == 'Fund Balance':
            # Direct balance setting - overwrite
            funds[fund_name] = {
                'amount': amount,
                'date': row.get('Date', '')
            }
        elif row_type == 'Fund Add':
            # Addition to fund - accumulate
            if fund_name in funds:
                funds[fund_name]['amount'] += amount
            else:
                funds[fund_name] = {
                    'amount': amount,
                    'date': row.get('Date', '')
                }

    return funds

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

# ============== UNIVERSAL UNDO HANDLER ==============

def perform_undo(channel_id):
    """Perform undo based on last action type"""
    action, error = get_undo_action(channel_id)
    
    if not action:
        return False, error
    
    action_type = action['type']
    data = action['data']
    
    if action_type == 'delete':
        success, result = undo_delete(data)
        if success:
            clear_undo_action(channel_id)
            if len(result) == 1:
                return True, f"↩️ Restored: {result[0]['category']} - {fmt(result[0]['amount'])}"
            return True, f"↩️ Restored {len(result)} items"
        return False, result
    
    elif action_type == 'add':
        success, result = delete_row_by_index(data['row_index'])
        if success:
            clear_undo_action(channel_id)
            return True, f"↩️ Removed last transaction"
        return False, result
    
    elif action_type == 'edit':
        success, result = undo_edit(data)
        if success:
            clear_undo_action(channel_id)
            return True, result
        return False, result
    
    elif action_type == 'paid':
        success, result = undo_paid(data)
        if success:
            clear_undo_action(channel_id)
            return True, f"↩️ Loan restored to unpaid"
        return False, result
    
    return False, "Unknown action type"

# ============== SLACK EVENT HANDLER ==============

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
    
    event_id = event.get('client_msg_id') or event.get('event_ts') or data.get('event_id')
    if is_duplicate_event(event_id):
        return jsonify({'ok': True})
    
    if event_type == 'message':
        channel = event.get('channel')
        text = event.get('text', '').strip()
        # Normalize Unicode to NFC form (composed) for Vietnamese characters
        text = unicodedata.normalize('NFC', text)
        user_id = event.get('user')

        user_name = detect_user_name(user_id)

        text_lower = text.lower()
        
        # Command: status
        if text_lower in ['status', 'tình hình', 'báo cáo', 'check']:
            funds = get_fund_status()
            summary = get_monthly_summary()
            has_loans = has_outstanding_loans()
            
            if funds or summary:
                msg = "📊 *Status Update*\n\n"
                
                if summary:
                    month_name = datetime.strptime(summary['month'], '%Y-%m-%d').strftime('%B %Y')
                    msg += f"*{month_name}:*\n"
                    msg += f"• Income: {fmt(summary['total_income'])}\n"
                    msg += f"• Expenses: {fmt(summary['total_expenses'])}\n"
                    net = summary['total_income'] - summary['total_expenses']
                    msg += f"• Net: {fmt(net)}\n\n"
                
                if funds:
                    msg += "*Fund Balances:*\n"
                    for fund, fdata in funds.items():
                        msg += f"• {fund}: {fmt(fdata['amount'])}\n"
                    
                    emergency = funds.get('Emergency Fund', {}).get('amount', 0)
                    if emergency:
                        progress = (emergency / 15000000) * 100
                        msg += f"\n🎯 Emergency Fund: {progress:.1f}% → ₩15M"
                
                if has_loans:
                    msg += "\n\n⚠️ Check Loan - Debt → `list debt`"
                
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text="❌ Cannot fetch status")
        
        # Command: bills
        elif text_lower in ['bills', 'fixed', 'fixed bills', 'fixbill', 'fix bill']:
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

        # Command: list joint
        elif text_lower in ['list joint', 'list chung', 'list quỹ chung']:
            transactions = get_all_transactions()
            joint_tx = [t for t in transactions if t['person'] == 'Joint' and t['type'] == 'Expense']
            joint_tx = sorted(joint_tx, key=lambda x: x['date'], reverse=True)[:20]
            if joint_tx:
                msg = format_transaction_list(joint_tx, "Joint Expenses", channel)
                # Add total at the end
                total = sum(t['amount'] for t in joint_tx)
                msg += f"\n\n💰 Total: {fmt(total)}"
            else:
                msg = "📋 No joint expenses found!"
            slack_client.chat_postMessage(channel=channel, text=msg)

        # Command: list debt / list loan (MUST be before general 'list' check)
        elif text_lower in ['list debt', 'list loan', 'list nợ', 'list mượn', 'debt', 'loan']:
            loans = get_outstanding_loans()
            if loans:
                last_debt_list[channel] = loans
                msg = format_transaction_list(loans, "Loan & Debt", channel, is_debt_list=True)
            else:
                msg = "📋 No outstanding loans/debts! 🎉"
            slack_client.chat_postMessage(channel=channel, text=msg)
        
        # Command: paid (mark loan as paid)
        elif text_lower.startswith('paid'):
            target_str = text_lower.replace('paid', '').strip()

            if not target_str:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `paid 1` or `paid 1,2,3`")
                return jsonify({'ok': True})

            # Parse targets (support single or comma-separated)
            targets = parse_delete_targets(target_str)

            if not targets:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `paid 1` or `paid 1,2,3`")
                return jsonify({'ok': True})

            # Process each loan payment
            paid_items = []
            undo_data_list = []

            for target in sorted(targets):
                loan_index = target - 1
                success, result, undo_data = mark_loan_as_paid(loan_index, channel)

                if success:
                    paid_items.append(result)
                    undo_data_list.append(undo_data)
                else:
                    slack_client.chat_postMessage(channel=channel, text=f"❌ {result}")
                    return jsonify({'ok': True})

            if paid_items:
                # Store for undo (all payments)
                store_undo_action(channel, 'paid', undo_data_list)

                if len(paid_items) == 1:
                    msg = f"✅ Paid: {fmt(paid_items[0]['amount'])} - {paid_items[0]['description']}\n"
                    msg += f"💰 Logged as income: nhận lại/trả nợ"
                else:
                    msg = f"✅ Paid {len(paid_items)} loans:\n"
                    for item in paid_items:
                        msg += f"  • {fmt(item['amount'])} - {item['description']}\n"
                    total = sum(item['amount'] for item in paid_items)
                    msg += f"\n💰 Total logged as income: {fmt(total)}"

                slack_client.chat_postMessage(channel=channel, text=msg)
        
        # Command: list (general)
        elif text_lower.startswith('list') or text_lower.startswith('last'):
            if text_lower.startswith('last'):
                words = text_lower.split()
                limit = int(words[1]) if len(words) > 1 and words[1].isdigit() else 5
                filter_type, filter_category, filter_person, filter_month, _ = None, None, None, None, limit
            else:
                filter_type, filter_category, filter_person, filter_month, limit = parse_list_command(text_lower)
            
            transactions = get_all_transactions()
            filtered = filter_transactions(transactions, filter_type, filter_category, filter_person, filter_month, limit)
            
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
            target_str = text_lower.replace('delete', '').strip()
            
            if not target_str:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `delete 1` or `delete 1,2,3` or `delete 1-5` or `delete last`")
                return jsonify({'ok': True})
            
            targets = parse_delete_targets(target_str)
            
            if not targets:
                slack_client.chat_postMessage(channel=channel, text="❓ Invalid format. Use: `delete 1` or `delete 1,2,3` or `delete 1-5`")
                return jsonify({'ok': True})
            
            success, message, deleted_items, deleted_rows_data = delete_transactions(targets, channel)
            
            if success:
                # Store for undo
                store_undo_action(channel, 'delete', deleted_rows_data)
                
                if len(deleted_items) == 1:
                    msg = f"🗑️ Deleted: {deleted_items[0]['category']} - {fmt(deleted_items[0]['amount'])}\n"
                else:
                    msg = f"🗑️ Deleted {len(deleted_items)} items:\n"
                    for item in deleted_items[:5]:
                        msg += f"  • {item['category']} - {fmt(item['amount'])}\n"
                    if len(deleted_items) > 5:
                        msg += f"  ... and {len(deleted_items) - 5} more\n"
                msg += "↩️ To undo: `undo`"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ {message}")
        
        # Command: edit
        elif text_lower.startswith('edit'):
            words = text.split()
            
            if len(words) < 3:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `edit 1 150K`")
                return jsonify({'ok': True})
            
            target = words[1]
            new_amount_str = words[2]
            
            if not target.isdigit():
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `edit 1 150K`")
                return jsonify({'ok': True})
            
            idx = int(target) - 1
            if channel not in last_list_results or idx >= len(last_list_results[channel]):
                slack_client.chat_postMessage(channel=channel, text="❌ Invalid number. Use `list` first")
                return jsonify({'ok': True})
            
            tx_to_edit = last_list_results[channel][idx]
            new_amount = parse_amount(new_amount_str)
            
            if not new_amount:
                slack_client.chat_postMessage(channel=channel, text="❌ Invalid amount")
                return jsonify({'ok': True})
            
            success, old_value, edit_data = edit_transaction(tx_to_edit['row_index'], new_amount)
            
            if success:
                # Store for undo
                store_undo_action(channel, 'edit', edit_data)
                
                msg = f"✏️ Updated: {tx_to_edit['category']}\n"
                msg += f"   {fmt(int(float(old_value)))} → {fmt(new_amount)}"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ Error: {old_value}")
        
        # Command: undo (universal)
        elif text_lower == 'undo':
            success, message = perform_undo(channel)
            slack_client.chat_postMessage(channel=channel, text=message if success else f"❌ {message}")
        
        # Command: help
        elif text_lower in ['help', 'trợ giúp', '?']:
            help_msg = """🤖 *Finance Bot V5.2*

*➕ Add Transaction:*
• `salary 2m` - Log income
• `50K cà phê` - Log expense
• `jacob 2.8M salary` - Specify person
• `gas dec 150K` - Backdate to month
• `50K cho sơn mượn` - Log loan

*📋 List:*
• `list` - This month
• `list expense` - Expenses only
• `list dec` - December
• `list joint` - Joint expenses
• `list emergency fund` - Emergency Fund additions
• `list debt` - Outstanding loans
• `last 5` - Last 5 transactions

*🗑️ Delete:*
• `delete 1` or `delete 1,2,3` or `delete 1-5`
• `delete last` or `delete last 3`

*💰 Loans:*
• `list debt` - See all loans
• `paid 1` or `paid 1,2,3` - Mark loans as repaid

*✏️ Edit:*
• `edit 1 150K` - Change amount

*↩️ Undo (works for any last action):*
• `undo` - Undo last add/delete/edit/paid

*📊 Status:*
• `status` - Summary + funds
• `bills` - Fixed bills

*💰 Quick Fund Add:*
• `quỹ khẩn cấp 1M` or `quy khan cap 1M` - Emergency Fund
• `quỹ đầu tư 500K` or `quy dau tu 500K` - Investment Fund
• `emergency fund 1M` - Emergency Fund (English)"""
            slack_client.chat_postMessage(channel=channel, text=help_msg)

        # Command: Quick fund add (Vietnamese/English)
        elif any(text_lower.startswith(unicodedata.normalize('NFC', prefix)) for prefix in [
            'quỹ khẩn cấp', 'quỹ đầu tư', 'quỹ kế hoạch', 'quỹ hẹn hò',
            'quy khan cap', 'quy dau tu', 'quy ke hoach', 'quy hen ho',
            'emergency fund', 'investment fund', 'planning fund', 'date fund',
            'thêm quỹ', 'them quy'
        ]):
            fund_mapping = {
                'quỹ khẩn cấp': ('Emergency Fund', '🎯'),
                'quy khan cap': ('Emergency Fund', '🎯'),
                'emergency fund': ('Emergency Fund', '🎯'),
                'quỹ đầu tư': ('Investment Fund', '📈'),
                'quy dau tu': ('Investment Fund', '📈'),
                'investment fund': ('Investment Fund', '📈'),
                'quỹ kế hoạch': ('Planning Fund', '🏠'),
                'quy ke hoach': ('Planning Fund', '🏠'),
                'planning fund': ('Planning Fund', '🏠'),
                'quỹ hẹn hò': ('Date Fund', '💕'),
                'quy hen ho': ('Date Fund', '💕'),
                'date fund': ('Date Fund', '💕'),
            }

            # Find which fund
            fund_name = None
            fund_emoji = '💰'
            for prefix, (name, emoji) in fund_mapping.items():
                if text_lower.startswith(unicodedata.normalize('NFC', prefix)):
                    fund_name = name
                    fund_emoji = emoji
                    break

            # Extract amount
            amount, _ = extract_amount_from_text(text)

            if not amount:
                slack_client.chat_postMessage(channel=channel, text="❓ Cách dùng:\n• `quỹ khẩn cấp 2M` = Thêm ₩2M vào quỹ\n• `emergency fund 500K` = Add ₩500K to fund")
                return jsonify({'ok': True})

            # Log to sheet as 'Fund Add'
            sheet = get_transaction_sheet()
            if sheet:
                now = datetime.now()
                row = [
                    now.strftime('%Y-%m-%d'),
                    'Fund Add',
                    fund_name,
                    amount,
                    f'Thêm vào {fund_name}',
                    user_name,
                    now.strftime('%Y-%m-01'),
                    'slack'
                ]
                sheet.append_row(row)

                # Store for undo
                all_values = sheet.get_all_values()
                new_row_index = len(all_values)
                store_undo_action(channel, 'add', {'row_index': new_row_index, 'row_data': row})

                # Get current fund balance (sum of all Fund Add + Fund Balance for this fund)
                funds = get_fund_status()
                old_balance = funds.get(fund_name, {}).get('amount', 0)
                new_balance = old_balance + amount

                # Progress for Emergency Fund
                progress_msg = ""
                if fund_name == 'Emergency Fund':
                    progress = (new_balance / 15000000) * 100
                    progress_msg = f"\n🎯 Tiến độ: {progress:.1f}% → ₩15M"

                msg = f"{fund_emoji} {fund_name} +{fmt(amount)}\n"
                msg += f"Số dư: {fmt(old_balance)} → {fmt(new_balance)}{progress_msg}"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text="❌ Không thể kết nối sheet")

        # Try to parse as transaction
        else:
            tx = parse_transaction(text, user_name)
            if tx:
                duplicate = check_duplicate_income(tx)
                
                success, msg, add_data = log_transaction(tx)
                if success:
                    # Store for undo
                    store_undo_action(channel, 'add', add_data)
                    
                    response = build_response(tx, duplicate_warning=duplicate)
                else:
                    response = f"❌ Error: {msg}"
                slack_client.chat_postMessage(channel=channel, text=response)
    
    return jsonify({'ok': True})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Couple Finance Bot V5.2 Final'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

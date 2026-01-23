import os
import re
import json
import random
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

# ============== DUPLICATE EVENT PREVENTION ==============
# Store last 100 processed event IDs to prevent duplicate processing
processed_events = set()
MAX_PROCESSED_EVENTS = 100

def is_duplicate_event(event_id):
    """Check if event was already processed"""
    global processed_events
    
    if not event_id:
        return False
    
    if event_id in processed_events:
        return True
    
    # Add to processed set
    processed_events.add(event_id)
    
    # Keep set size manageable
    if len(processed_events) > MAX_PROCESSED_EVENTS:
        # Remove oldest entries (convert to list, slice, convert back)
        processed_events = set(list(processed_events)[-50:])
    
    return False

# ============== STORAGE FOR UNDO/LIST ==============
last_deleted = {}  # Store deleted items for undo
last_list_results = {}  # Store list results for delete by number

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
                     'cho mượn', 'mượn', 'trả nợ', 'vay', 'nợ', 'trả lại', 'cho vay'],
        'emoji': ['💸', '🤝', '📝'],
        'responses': ["Noted! 📝", "Good to track this 💸", "Money matters! 🤝"]
    },
    'Business': {
        'keywords': ['ads', 'contractor', 'client', 'marketing', 'revenue', 'business',
                     'quảng cáo', 'cộng tác viên', 'khách hàng', 'doanh thu', 'công việc',
                     'ad spend', 'facebook ads', 'campaign'],
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
        'keywords': ['salary', 'commission', 'bonus', 'income', 'fee', 'revenue', 'wage', 'pay',
                     'lương', 'hoa hồng', 'thưởng', 'thu nhập', 'tiền lương'],
        'emoji': ['💰', '🎉', '💵'],
        'responses': ["Money in! 💰", "Cha-ching! 🎉", "Nice! Keep it coming! 💪", "Pay day! 💵"]
    },
}

INCOME_KEYWORDS = ['salary', 'commission', 'bonus', 'income', 'fee', 'revenue', 'wage', 'pay',
                   'lương', 'hoa hồng', 'thưởng', 'thu nhập', 'tiền lương', 'ad management fee']

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
    text_lower = text.lower()
    
    if category == 'Income':
        return True
    
    for keyword in INCOME_KEYWORDS:
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

def get_personality_response(category, category_data, amount, is_income):
    if random.random() > 0.5:
        return ""
    
    responses = category_data.get('responses', ["Logged! 📝"])
    
    if is_income and amount >= 5000000:
        return random.choice(["🎊 WOW! Amazing! 🚀", "Big income! 💰💰💰", "Incredible! Keep it up! 🔥"])
    
    if not is_income and amount >= 1000000:
        return random.choice(["Big purchase! 🛒", "That's a big one! 💸"])
    
    return random.choice(responses)

def get_emoji(category, category_data, is_income):
    if is_income:
        return random.choice(['💰', '💵', '🎉'])
    return random.choice(category_data.get('emoji', ['📝']))

# ============== DUPLICATE INCOME CHECK ==============

def check_duplicate_income(tx_data):
    """Check if similar income was logged recently (same amount, same day, same type)"""
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
            # Check if description is similar (contains same keywords)
            row_desc = str(row.get('Description', '')).lower()
            if (description_lower in row_desc or 
                row_desc in description_lower or
                'lương' in description_lower and 'lương' in row_desc or
                'salary' in description_lower and 'salary' in row_desc or
                'commission' in description_lower and 'commission' in row_desc):
                return row
    
    return None

# ============== TRANSACTION PARSING ==============

def parse_transaction(text, user_name):
    original_text = text.strip()
    
    text, year, month, is_backdated = extract_month_from_text(original_text)
    person, text = extract_person_from_text(text)
    if not person:
        person = user_name
    
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
        category, category_data = detect_category(description)
    
    tx_is_income = is_income(description, category)
    
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
        'is_backdated': is_backdated
    }

# ============== TRANSACTION LOGGING ==============

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
        tx_data['category'],
        tx_data['amount'],
        tx_data['description'],
        tx_data['person'],
        month_start,
        'slack'
    ]
    
    sheet.append_row(row)
    return True, "Transaction logged!"

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
    
    emoji = get_emoji(category, category_data, is_income_tx)
    
    response = f"{emoji} Logged: {category} - {fmt(amount)}\n"
    response += f"📝 {description}\n"
    
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
    
    # Add duplicate warning if exists
    if duplicate_warning:
        response += f"\n\n⚠️ *Warning:* You already logged {fmt(amount)} \"{description}\" today!"
        response += "\nDuplicate? Use `delete last` to remove."
    else:
        # Add personality only if no warning
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

def format_transaction_list(transactions, title, channel_id):
    if not transactions:
        return "📋 No transactions found."
    
    last_list_results[channel_id] = transactions
    
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
        
        msg += f"{i}. {emoji} {date_display} | {tx['category']} | {fmt(amount)} | {tx['person']}\n"
    
    if len(transactions) > 20:
        msg += f"\n... and {len(transactions) - 20} more"
    
    msg += f"\n\n*Delete:* `delete 1` or `delete 1,2,3` or `delete 1-5`"
    
    return msg

def parse_delete_targets(target_str):
    """
    Parse delete targets from string like:
    - "3" -> [3]
    - "3,4,5" -> [3, 4, 5]
    - "3-7" -> [3, 4, 5, 6, 7]
    - "1,3,5-8" -> [1, 3, 5, 6, 7, 8]
    - "last" -> ['last']
    - "last 3" -> ['last', 3]
    """
    targets = []
    
    if target_str.startswith('last'):
        parts = target_str.split()
        if len(parts) > 1 and parts[1].isdigit():
            return ['last', int(parts[1])]
        return ['last']
    
    # Split by comma
    parts = target_str.replace(' ', '').split(',')
    
    for part in parts:
        if '-' in part and not part.startswith('-'):
            # Range like "3-7"
            range_parts = part.split('-')
            if len(range_parts) == 2 and range_parts[0].isdigit() and range_parts[1].isdigit():
                start = int(range_parts[0])
                end = int(range_parts[1])
                targets.extend(range(start, end + 1))
        elif part.isdigit():
            targets.append(int(part))
    
    # Remove duplicates and sort in reverse (delete from bottom up to preserve indices)
    return sorted(list(set(targets)), reverse=True)

def delete_transactions(targets, channel_id):
    """Delete multiple transactions"""
    global last_deleted
    
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", []
    
    transactions = get_all_transactions()
    
    # Handle "last" or "last N"
    if targets and targets[0] == 'last':
        count = targets[1] if len(targets) > 1 else 1
        sorted_tx = sorted(transactions, key=lambda x: x['date'], reverse=True)
        targets = [i + 1 for i in range(min(count, len(sorted_tx)))]
        # Update list results for proper indexing
        last_list_results[channel_id] = sorted_tx
    
    if channel_id not in last_list_results:
        return False, "Use `list` first to see transactions", []
    
    list_results = last_list_results[channel_id]
    deleted_items = []
    deleted_rows_data = []
    
    # Validate all targets first
    for idx in targets:
        if idx < 1 or idx > len(list_results):
            return False, f"Invalid number: {idx}. Use `list` first.", []
    
    # Sort targets in reverse order (delete from bottom up)
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
            
            # Adjust row indices for remaining items
            for item in list_results:
                if item['row_index'] > tx['row_index']:
                    item['row_index'] -= 1
        
        # Store for undo
        last_deleted[channel_id] = {
            'items': deleted_rows_data,
            'timestamp': datetime.now()
        }
        
        return True, "Deleted successfully", deleted_items
    
    except Exception as e:
        return False, str(e), []

def undo_delete(channel_id):
    global last_deleted
    
    if channel_id not in last_deleted:
        return False, "Nothing to undo", []
    
    deleted_info = last_deleted[channel_id]
    time_diff = (datetime.now() - deleted_info['timestamp']).seconds
    if time_diff > 300:
        return False, "Undo expired (>5 minutes)", []
    
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets", []
    
    try:
        restored = []
        for item in deleted_info['items']:
            sheet.append_row(item['row_data'])
            restored.append(item['tx'])
        
        del last_deleted[channel_id]
        return True, "Restored successfully", restored
    
    except Exception as e:
        return False, str(e), []

def edit_transaction(row_index, new_amount):
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        old_value = sheet.cell(row_index, 4).value
        sheet.update_cell(row_index, 4, new_amount)
        return True, old_value
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
        if row.get('Type') == 'Fund Balance':
            fund_name = row.get('Category', '')
            funds[fund_name] = {
                'amount': row.get('Amount', 0),
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
    
    # Skip bot messages
    if event.get('bot_id'):
        return jsonify({'ok': True})
    
    # ===== DUPLICATE EVENT CHECK =====
    event_id = event.get('client_msg_id') or event.get('event_ts') or data.get('event_id')
    if is_duplicate_event(event_id):
        return jsonify({'ok': True})  # Skip duplicate
    
    if event_type == 'message':
        channel = event.get('channel')
        text = event.get('text', '').strip()
        user_id = event.get('user')
        
        try:
            user_info = slack_client.users_info(user=user_id)
            user_name = user_info['user']['real_name'].split()[0]
            if 'naomi' in user_name.lower() or 'nao' in user_name.lower() or 'thương' in user_name.lower():
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
                    for fund, fdata in funds.items():
                        msg += f"• {fund}: {fmt(fdata['amount'])}\n"
                    
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
        
        # Command: delete (supports multiple)
        elif text_lower.startswith('delete'):
            target_str = text_lower.replace('delete', '').strip()
            
            if not target_str:
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `delete 1` or `delete 1,2,3` or `delete 1-5` or `delete last`")
                return jsonify({'ok': True})
            
            targets = parse_delete_targets(target_str)
            
            if not targets:
                slack_client.chat_postMessage(channel=channel, text="❓ Invalid format. Use: `delete 1` or `delete 1,2,3` or `delete 1-5`")
                return jsonify({'ok': True})
            
            success, message, deleted_items = delete_transactions(targets, channel)
            
            if success:
                if len(deleted_items) == 1:
                    msg = f"🗑️ Deleted: {deleted_items[0]['category']} - {fmt(deleted_items[0]['amount'])}\n"
                else:
                    msg = f"🗑️ Deleted {len(deleted_items)} items:\n"
                    for item in deleted_items[:5]:  # Show max 5
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
            
            success, old_value = edit_transaction(tx_to_edit['row_index'], new_amount)
            
            if success:
                msg = f"✏️ Updated: {tx_to_edit['category']}\n"
                msg += f"   {fmt(int(float(old_value)))} → {fmt(new_amount)}"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ Error: {old_value}")
        
        # Command: undo
        elif text_lower == 'undo':
            success, message, restored = undo_delete(channel)
            
            if success:
                if len(restored) == 1:
                    msg = f"↩️ Restored: {restored[0]['category']} - {fmt(restored[0]['amount'])}"
                else:
                    msg = f"↩️ Restored {len(restored)} items"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ {message}")
        
        # Command: help
        elif text_lower in ['help', 'trợ giúp', '?']:
            help_msg = """🤖 *Finance Bot V5.1*

*➕ Add Transaction:*
• `salary 2m` - Log income
• `50K cà phê` - Log expense
• `jacob 2.8M salary` - Specify person
• `gas dec 150K` - Backdate to month

*📋 List:*
• `list` - This month
• `list dec` - December
• `list gas 5` - Last 5 gas bills
• `last 5` - Last 5 transactions

*🗑️ Delete (single or multiple):*
• `delete 1` - Delete item #1
• `delete 1,2,3` - Delete multiple
• `delete 1-5` - Delete range
• `delete last` - Delete most recent
• `delete last 3` - Delete last 3

*✏️ Edit & Undo:*
• `edit 1 150K` - Change amount
• `undo` - Restore deleted items

*📊 Status:*
• `status` - Summary + funds
• `bills` - Fixed bills"""
            slack_client.chat_postMessage(channel=channel, text=help_msg)
        
        # Try to parse as transaction
        else:
            tx = parse_transaction(text, user_name)
            if tx:
                # Check for duplicate income
                duplicate = check_duplicate_income(tx)
                
                success, msg = log_transaction(tx)
                if success:
                    response = build_response(tx, duplicate_warning=duplicate)
                else:
                    response = f"❌ Error: {msg}"
                slack_client.chat_postMessage(channel=channel, text=response)
    
    return jsonify({'ok': True})

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'bot': 'Couple Finance Bot V5.1'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)

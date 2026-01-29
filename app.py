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
        'keywords': [
            # English
            'eat', 'dinner', 'lunch', 'breakfast', 'brunch', 'restaurant', 'coffee', 'cafe', 'meal', 'food',
            'takeout', 'takeaway', 'dine', 'dining', 'drinks', 'beverage', 'beer', 'wine', 'juice', 'smoothie',
            'dessert', 'cake', 'pizza', 'burger', 'sushi', 'noodles', 'soup', 'snack', 'chicken', 'bbq',
            # Vietnamese
            'ăn', 'cơm', 'phở', 'bún', 'bánh mì', 'cà phê', 'cafe', 'nhà hàng', 'ăn trưa', 'ăn tối',
            'ăn sáng', 'quán', 'gọi đồ ăn', 'delivery', 'đặt đồ ăn', 'ăn vặt', 'trà sữa', 'kem',
            'lẩu', 'nướng', 'thịt nướng', 'samgyupsal', 'chimaek', 'gà rán', 'gà', 'thịt', 'cá',
            'bún bò', 'bún chả', 'bánh cuốn', 'chè', 'đồ ăn', 'mì', 'hủ tiếu', 'cháo', 'xôi',
            'bánh bao', 'bánh', 'hải sản', 'tôm', 'cua', 'sushi', 'kimbap', 'đồ uống', 'nước ngọt',
            'bia', 'rượu', 'cocktail', 'nhậu', 'ăn nhậu', 'buffet', 'tiệc', 'nước ép', 'sinh tố',
            'trà', 'matcha', 'bánh ngọt', 'tráng miệng', 'ăn chơi', 'ăn uống', 'đi ăn',
            # Korean
            '치킨', '커피', '식당', '밥',
        ],
        'emoji': ['🍜', '☕', '🍕', '🍔', '🍱'],
        'responses': ["Yummy! 😋", "맛있게 드세요!", "Ăn ngon nha!", "Enjoy your meal! 🍴", "Tasty! 😄"]
    },
    'Groceries': {
        'keywords': [
            # English
            'grocery', 'groceries', 'market', 'supermarket', 'mart', 'vegetables', 'fruits', 'meat',
            'fish', 'rice', 'eggs', 'milk', 'bread', 'snacks', 'water', 'oil', 'salt', 'sugar',
            'spices', 'frozen', 'canned', 'produce', 'dairy', 'bakery',
            # Vietnamese
            'đi chợ', 'siêu thị', 'thực phẩm', 'rau', 'thịt', 'trứng', 'sữa', 'gạo', 'chợ',
            'rau củ quả', 'rau củ', 'củ quả', 'đường', 'muối', 'dầu ăn', 'nước mắm', 'gia vị',
            'bột', 'mì gói', 'đồ khô', 'hoa quả', 'trái cây', 'cam', 'táo', 'chuối', 'nho',
            'dưa hấu', 'xoài', 'bánh kẹo', 'đồ ăn vặt', 'nước uống', 'nước lọc', 'nước suối',
            'mua rau', 'mua thịt', 'mua đồ', 'đồ gia dụng', 'giấy vệ sinh', 'xà phòng', 'bột giặt',
            'nước rửa chén', 'nước rửa bát', 'khăn giấy', 'tã', 'sữa tắm', 'dầu gội',
            # Korean stores
            'coupang', '쿠팡', 'emart', 'homeplus', 'lotte mart', '이마트', '홈플러스', '롯데마트',
        ],
        'emoji': ['🛒', '🥬', '🥚'],
        'responses': ["Stocking up! 🛒", "Coupang delivery? 📦", "Fresh groceries! 🥬"]
    },
    'Transport': {
        'keywords': [
            # English
            'grab', 'taxi', 'bus', 'subway', 'train', 'ktx', 'parking', 'toll', 'ride', 'uber', 'lyft',
            'gas', 'petrol', 'fuel', 'car wash', 'maintenance', 'repair', 'ticket', 'fare', 'transit',
            # Vietnamese
            'xe', 'xe buýt', 'tàu điện', 'gửi xe', 'đỗ xe', 'phí cầu đường', 'xăng', 'đổ xăng',
            'đi lại', 'đi xe', 'tiền xe', 'vé xe', 'xe máy', 'ô tô', 'xe hơi', 'sửa xe', 'rửa xe',
            'bảo dưỡng', 'phí giao thông', 'cầu đường', 'phà', 'tàu', 'máy bay', 'vé tàu',
            'tiền xăng', 'tiền gửi xe', 'bãi xe', 'phí đỗ xe',
            # Korean
            '택시', '지하철', 'kakao taxi', '카카오택시', '버스', '주유',
        ],
        'emoji': ['🚕', '🚇', '🚗'],
        'responses': ["Safe travels! 🚗", "Đi cẩn thận nha!", "On the move! 🚇"]
    },
    'Gift': {
        'keywords': [
            # English
            'gift', 'present', 'wedding gift', 'birthday', 'baby shower', 'graduation', 'christmas',
            'holiday', 'celebration', 'party', 'surprise',
            # Vietnamese
            'quà', 'tặng', 'quà cưới', 'mừng cưới', 'quà sinh nhật', 'sinh nhật', 'đám cưới',
            'thôi nôi', 'quà tân gia', 'tặng bạn', 'mừng', 'quà tặng', 'tặng quà', 'mừng sinh nhật',
            'tiệc', 'lễ', 'quà noel', 'quà tết', 'lì xì', 'tiền mừng', 'phong bì',
            # Korean
            '돌잔치', '선물', '축하',
        ],
        'emoji': ['🎁', '💝', '🎀'],
        'responses': ["So thoughtful! 💕", "Người nhận sẽ vui lắm!", "Nice gift! 🎁", "Generous! 💝"]
    },
    'Family Support': {
        'keywords': [
            # English
            'mom', 'dad', 'parents', 'family', 'send home', 'remittance', 'support family',
            # Vietnamese
            'cho mẹ', 'cho ba', 'biếu', 'hỗ trợ gia đình', 'gửi về', 'gửi tiền', 'tiền nhà',
            'bố mẹ', 'gia đình', 'cho bố', 'mẹ', 'ba', 'bố', 'biếu bố mẹ', 'gửi về nhà',
            'tiền gửi về', 'chu cấp', 'nuôi gia đình', 'cho ông bà', 'ông', 'bà', 'anh chị em',
        ],
        'emoji': ['👨‍👩‍👧', '❤️', '🏠'],
        'responses': ["Family first! ❤️", "Hiếu thảo quá! 👏", "Family love! 👨‍👩‍👧"]
    },
    'Date': {
        'keywords': [
            # English
            'date', 'dating', 'couple', 'anniversary', 'romantic', 'valentine', 'love',
            # Vietnamese
            'hẹn hò', 'kỷ niệm', 'lãng mạn', 'đi chơi hai đứa', 'tình yêu', 'hai đứa',
            'đi date', 'ngày kỷ niệm', 'valentine', 'đi chơi cùng', 'với bé', 'với anh', 'với em',
        ],
        'emoji': ['💑', '🥰', '💕'],
        'responses': ["Enjoy your date! 💕", "Have fun you two! 🥰", "Love is in the air! 💑"]
    },
    'Entertainment': {
        'keywords': [
            # English
            'movie', 'game', 'netflix', 'concert', 'karaoke', 'pc bang', 'show', 'music', 'festival',
            'park', 'zoo', 'museum', 'arcade', 'bowling', 'billiards', 'pool', 'sports', 'gym', 'fitness',
            'cinema', 'theater', 'play', 'ticket', 'event',
            # Vietnamese
            'phim', 'xem phim', 'giải trí', 'game', 'youtube', 'spotify', 'xem show', 'ca nhạc',
            'nhạc hội', 'lễ hội', 'công viên', 'du ngoạn', 'chụp ảnh', 'studio', 'spa', 'massage',
            'làm đẹp', 'làm nail', 'làm tóc', 'cắt tóc', 'nhuộm tóc', 'rạp phim', 'rạp chiếu phim',
            'đi chơi', 'vui chơi', 'thư giãn', 'nghỉ ngơi',
            # Korean
            '노래방', 'pc방', '영화', '게임',
        ],
        'emoji': ['🎬', '🎮', '🎤'],
        'responses': ["Have fun! 🎉", "Giải trí xíu! 🎬", "Enjoy! 🎮"]
    },
    'Shopping': {
        'keywords': [
            # English
            'buy', 'purchase', 'clothes', 'shoes', 'daiso', 'olive young', 'shop', 'shopping',
            'electronics', 'phone', 'laptop', 'headphones', 'accessories', 'home', 'kitchen',
            'decor', 'furniture', 'online shopping', 'amazon', 'fashion', 'style', 'wear',
            # Vietnamese
            'mua', 'quần áo', 'giày dép', 'mỹ phẩm', 'skincare', 'mua sắm', 'đồ', 'áo', 'quần',
            'đồ dùng', 'vật dụng', 'đồ gia dụng', 'điện tử', 'điện thoại', 'laptop', 'máy tính',
            'tai nghe', 'đồ decor', 'trang trí', 'nội thất', 'đồ bếp', 'chén bát', 'xoong nồi',
            'váy', 'đầm', 'túi xách', 'balo', 'ví', 'đồng hồ', 'trang sức', 'phụ kiện',
            'son', 'kem dưỡng', 'serum', 'toner', 'sữa rửa mặt', 'makeup', 'trang điểm',
            # Korean
            '다이소', '올리브영', '쇼핑', '옷',
        ],
        'emoji': ['🛍️', '👗', '👟'],
        'responses': ["Treat yourself! 🛍️", "Shopping therapy! 💅", "Nice buy! 👍"]
    },
    'Travel': {
        'keywords': [
            # English
            'flight', 'ticket', 'hotel', 'travel', 'trip', 'airbnb', 'booking', 'vacation',
            'holiday', 'tour', 'visa', 'passport', 'luggage', 'suitcase', 'airport',
            # Vietnamese
            'vé máy bay', 'vé', 'khách sạn', 'du lịch', 'về việt nam', 'về quê', 'bay', 'book',
            'đặt phòng', 'resort', 'nghỉ dưỡng', 'đi chơi xa', 'nghỉ mát', 'biển', 'núi',
            'sân bay', 'hành lý', 'vali', 'visa', 'hộ chiếu', 'tour', 'đặt tour',
            # Korean
            '여행', '비행기', '호텔',
        ],
        'emoji': ['✈️', '🧳', '🏖️'],
        'responses': ["Bon voyage! ✈️", "Safe travels!", "Du lịch vui nha! 🌴", "Về quê! 🇻🇳❤️"]
    },
    'Healthcare': {
        'keywords': [
            # English
            'doctor', 'hospital', 'medicine', 'pharmacy', 'clinic', 'health', 'dental', 'eye',
            'glasses', 'checkup', 'test', 'lab', 'insurance', 'supplements', 'treatment',
            # Vietnamese
            'bác sĩ', 'thuốc', 'bệnh viện', 'khám bệnh', 'hiệu thuốc', 'vitamin', 'sick', 'ốm',
            'bệnh', 'khám', 'chữa bệnh', 'xét nghiệm', 'siêu âm', 'nha khoa', 'răng', 'mắt',
            'kính', 'thuốc bổ', 'thực phẩm chức năng', 'bảo hiểm y tế', 'tiêm', 'vaccine',
            'nhổ răng', 'trám răng', 'khám mắt', 'thuốc men', 'y tế', 'sức khỏe',
            # Korean
            '병원', '약국', '약', '의사',
        ],
        'emoji': ['💊', '🏥', '💪'],
        'responses': ["Health is wealth! 💪", "Get well soon!", "Take care! 🏥"]
    },
    'Loan & Debt': {
        'keywords': [
            # English
            'lend', 'borrow', 'debt', 'loan', 'repay', 'pay back', 'lending', 'owed', 'owe',
            # Vietnamese
            'cho mượn', 'mượn', 'trả nợ', 'vay', 'nợ', 'trả lại', 'cho vay', 'thiếu', 'trả tiền',
            'mượn tiền', 'cho vay tiền', 'nợ tiền', 'trả nợ tiền', 'đòi nợ', 'thu nợ',
        ],
        'emoji': ['🤝', '💸', '📝'],
        'responses': ["Loan tracked! 🤝", "Don't forget to follow up! 📝", "Noted! 💸"]
    },
    'Business': {
        'keywords': [
            # English
            'ads', 'contractor', 'client', 'marketing', 'revenue', 'business', 'ad spend',
            'facebook ads', 'campaign', 'google ads', 'tiktok ads', 'advertising', 'promotion',
            # Vietnamese
            'quảng cáo', 'cộng tác viên', 'khách hàng', 'doanh thu', 'công việc', 'kinh doanh',
            'chi phí quảng cáo', 'chạy ads', 'thuê người', 'nhân viên', 'lương nhân viên',
            # Specific people
            'chị dương', 'chi duong', 'dương', 'duong',
            'gởi jacob', 'goi jacob', 'tiền jacob', 'tien jacob', 'jacob fee',
        ],
        'emoji': ['💼', '📈', '💹'],
        'responses': ["Business expense logged! 💼", "Invest to grow! 📈", "Business moves! 💹"]
    },
    'Subscription': {
        'keywords': [
            # English
            'subscription', 'monthly', 'netflix', 'spotify', 'claude', 'chatgpt', 'premium',
            'membership', 'annual', 'yearly', 'plan', 'tier',
            # Vietnamese
            'đăng ký', 'gói tháng', 'youtube premium', 'disney', 'apple', 'gói năm', 'thuê bao',
            'phí hàng tháng', 'gia hạn', 'renew',
        ],
        'emoji': ['📱', '💳', '🔄'],
        'responses': ["Subscription noted! 📱", "Monthly fee logged! 💳"]
    },
    'Housing': {
        'keywords': [
            # English
            'rent', 'deposit', 'maintenance', 'apartment', 'house', 'utilities', 'electric',
            'water bill', 'gas bill', 'internet bill', 'wifi',
            # Vietnamese
            'tiền nhà', 'thuê nhà', 'đặt cọc', 'bảo trì', 'nhà', 'phòng', 'tiền điện', 'tiền nước',
            'tiền gas', 'tiền mạng', 'tiền wifi', 'phí chung cư', 'phí quản lý',
            # Korean
            '관리비', '월세', '전세', '집세',
        ],
        'emoji': ['🏠', '🔑', '🏢'],
        'responses': ["Home sweet home! 🏠", "Housing cost noted! 🔑"]
    },
    'Education': {
        'keywords': [
            # English
            'course', 'class', 'book', 'study', 'korean class', 'learn', 'school', 'tuition',
            'university', 'college', 'training', 'workshop', 'seminar', 'certificate',
            # Vietnamese
            'học', 'khóa học', 'lớp', 'sách', 'học tiếng hàn', 'tiếng hàn', 'học phí', 'trường',
            'đại học', 'cao đẳng', 'đào tạo', 'workshop', 'hội thảo', 'chứng chỉ', 'thi', 'ôn thi',
            # Korean
            '한국어', '학원', '수업',
        ],
        'emoji': ['📚', '🎓', '✏️'],
        'responses': ["Invest in yourself! 📚", "Knowledge is power! 🎓", "Keep learning! ✏️"]
    },
    'Pet': {
        'keywords': [
            # English
            'pet', 'cat', 'dog', 'vet', 'pet food', 'pet supplies', 'grooming', 'puppy', 'kitten',
            # Vietnamese
            'mèo', 'chó', 'thú cưng', 'thú y', 'đồ cho mèo', 'đồ cho chó', 'thức ăn cho mèo',
            'thức ăn cho chó', 'cát vệ sinh', 'lồng', 'dây xích', 'vòng cổ', 'đồ chơi cho pet',
        ],
        'emoji': ['🐱', '🐕', '🐾'],
        'responses': ["For the fur baby! 🐾", "Pet parent life! 🐱"]
    },
    'Income': {
        'keywords': [
            # English
            'salary', 'commission', 'bonus', 'income', 'revenue', 'wage', 'pay', 'paycheck',
            'earnings', 'profit', 'dividend', 'interest', 'refund', 'cashback',
            # Vietnamese
            'lương', 'hoa hồng', 'thưởng', 'thu nhập', 'tiền lương', 'tiền công', 'tiền thưởng',
            'lãi', 'hoàn tiền', 'nhận tiền', 'được trả', 'tiền về',
        ],
        'emoji': ['💰', '🎉', '💵'],
        'responses': ["Money in! 💰", "Cha-ching! 🎉", "Nice! Keep it coming! 💪", "Pay day! 💵"]
    },
}

# ============== BUDGET & SETTINGS ==============
DEFAULT_BUDGETS = {
    'Food & Dining': 200000,
    'Groceries': 500000,
    'Entertainment': 100000,
    'Shopping': 300000,
    'Transport': 200000,
    'Business': 2000000,
    'Healthcare': 200000,
    'Gift': 300000,
}

WISDOM_POOL = {
    'saving': [
        '"Của bền tại người" - Tiết kiệm hôm nay, tự do ngày mai 🌟',
        '"Tích tiểu thành đại" - Góp gió thành bão 🌊',
        '"Kiến tha lâu cũng đầy tổ" 🐜',
        '"Do not save what is left after spending, spend what is left after saving." - Warren Buffett',
    ],
    'spending': [
        '"Khéo ăn thì no, khéo co thì ấm"',
        '"Làm khi lành, để dành khi đau"',
        '"Beware of little expenses. A small leak will sink a great ship." - Benjamin Franklin',
    ],
    'income': [
        '"Có làm thì mới có ăn" 💪',
        '"Tay làm hàm nhai, tay quai miệng trễ"',
        '"The harder you work, the luckier you get."',
    ],
    'milestone': [
        '"Có công mài sắt, có ngày nên kim" ⚒️',
        '"Một bước chân, nghìn dặm đường" 🎯',
        '"Kiên nhẫn là mẹ thành công"',
        '"The journey of a thousand miles begins with a single step." - Lao Tzu',
    ],
    'over_budget': [
        '"Liệu cơm gắp mắm" 🍚',
        '"Vung tay quá trán" - Cẩn thận nha!',
        '"Năng nhặt chặt bị" - Tiết kiệm từng chút một',
    ],
}

EMERGENCY_FUND_MILESTONES = [5000000, 7500000, 10000000, 12500000, 15000000]  # ₩5M, ₩7.5M, ₩10M, ₩12.5M, ₩15M

CELEBRATION_MESSAGES = {
    'income': [
        "💰 Ngày lương! Tuyệt vời!",
        "💵 Tiền vào! Keep it coming! 💪",
        "🎉 Cha-ching! Làm tốt lắm!",
    ],
    'big_income': [
        "🎊 WOW! Thu nhập khủng! 🚀",
        "💰💰💰 Jackpot! Quá đỉnh!",
        "🔥 Big money! Cứ thế phát huy!",
    ],
    'milestone_5m': [
        "🎯 MILESTONE! Emergency Fund đạt ₩5M!",
        "33% đường đến tự do tài chính! 💪",
    ],
    'milestone_7.5m': [
        "🎯 MILESTONE! Emergency Fund đạt ₩7.5M!",
        "Halfway there! 50% rồi! 🔥",
    ],
    'milestone_10m': [
        "🎊 MILESTONE LỚN! Emergency Fund đạt ₩10M!",
        "67% đường đến ₩15M! Quá xuất sắc! 🏆",
    ],
    'milestone_12.5m': [
        "🚀 SẮP ĐẾN ĐÍCH! Emergency Fund đạt ₩12.5M!",
        "Chỉ còn ₩2.5M nữa thôi! 🎯",
    ],
    'milestone_15m': [
        "🎊🎉🏆 FREEDOM ACHIEVED! ₩15M! 🏆🎉🎊",
        "Bạn đã đạt được TỰ DO TÀI CHÍNH!",
        "\"Có công mài sắt, có ngày nên kim\" - Hôm nay là ngày đó! 🌟",
    ],
    'under_budget': [
        "👏 Tháng này tiết kiệm hơn tháng trước!",
        "📉 Chi tiêu giảm! Làm tốt lắm!",
    ],
}

SETTINGS_DEFAULTS = {
    'tone': 'vietnamese_mix',
    'wisdom_frequency': 50,
    'celebrations': True,
    'warnings': True,
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
    amount_str = str(amount_str).replace('₩', '').replace(' ', '').strip()

    # Handle Vietnamese decimal notation (15,5k = 15.5k, 1,5m = 1.5m)
    # If format is like "15,5k" or "1,5m", convert comma to dot
    if re.match(r'^\d+,\d+[mkMK]$', amount_str):
        amount_str = amount_str.replace(',', '.')
    else:
        # Remove commas used as thousand separators (1,000,000)
        amount_str = amount_str.replace(',', '')

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

# ============== BUDGET & WISDOM FUNCTIONS ==============

def get_monthly_spending_by_category(month=None):
    """Get total spending per category for a month"""
    sheet = get_transaction_sheet()
    if not sheet:
        return {}

    if not month:
        month = datetime.now().strftime('%Y-%m-01')

    records = sheet.get_all_records()
    spending = {}

    for row in records:
        row_month = str(row.get('Month', ''))[:10]
        if row_month != month:
            continue

        if row.get('Type') != 'Expense':
            continue

        category = row.get('Category', 'Other')
        amount = row.get('Amount', 0) or 0

        spending[category] = spending.get(category, 0) + amount

    return spending

def check_budget_warning(category, amount):
    """Check if spending triggers a budget warning"""
    budget = DEFAULT_BUDGETS.get(category, 0)
    if budget == 0:
        return None, None

    # Get current month spending
    spending = get_monthly_spending_by_category()
    current_spent = spending.get(category, 0)
    total_spent = current_spent + amount  # Including this new transaction

    percentage = (total_spent / budget) * 100
    remaining = budget - total_spent

    if percentage >= 100:
        # Over budget - strong warning
        over_amount = total_spent - budget
        return 'over', {
            'budget': budget,
            'spent': total_spent,
            'over': over_amount,
            'percentage': percentage,
        }
    elif percentage >= 80:
        # Near budget - gentle warning
        return 'warning', {
            'budget': budget,
            'spent': total_spent,
            'remaining': remaining,
            'percentage': percentage,
        }
    else:
        # Under budget - show remaining
        return 'ok', {
            'budget': budget,
            'spent': total_spent,
            'remaining': remaining,
            'percentage': percentage,
        }

def get_wisdom(context='saving'):
    """Get random wisdom based on context, 50% chance"""
    if random.random() > 0.5:
        return None

    quotes = WISDOM_POOL.get(context, WISDOM_POOL['saving'])
    return random.choice(quotes)

def check_milestone(fund_name, old_balance, new_balance):
    """Check if a milestone was crossed"""
    if fund_name != 'Emergency Fund':
        return None

    for milestone in EMERGENCY_FUND_MILESTONES:
        if old_balance < milestone <= new_balance:
            # Crossed a milestone!
            if milestone == 5000000:
                return random.choice(CELEBRATION_MESSAGES['milestone_5m'])
            elif milestone == 7500000:
                return random.choice(CELEBRATION_MESSAGES['milestone_7.5m'])
            elif milestone == 10000000:
                return random.choice(CELEBRATION_MESSAGES['milestone_10m'])
            elif milestone == 12500000:
                return random.choice(CELEBRATION_MESSAGES['milestone_12.5m'])
            elif milestone == 15000000:
                return random.choice(CELEBRATION_MESSAGES['milestone_15m'])

    return None

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
    """Undo a paid action - remove [PAID] prefix and delete income entry"""
    sheet = get_transaction_sheet()
    if not sheet:
        return False, "Cannot connect to Google Sheets"
    
    try:
        # 1. Restore original description (remove [PAID] prefix)
        sheet.update_cell(undo_data['loan_row_index'], 5, undo_data['original_desc'])
        
        # 2. Find and delete the income entry
        records = sheet.get_all_records()
        for i, row in enumerate(records):
            if (row.get('Type') == 'Income' and 
                row.get('Category') == 'Loan & Debt' and
                f"nhận lại/trả nợ: {undo_data['original_desc']}" in str(row.get('Description', ''))):
                sheet.delete_rows(i + 2)
                break
        
        return True, "Paid action undone"
    except Exception as e:
        return False, str(e)

# ============== TRANSACTION PARSING ==============

def parse_transaction(text, user_name):
    original_text = text.strip()

    text, year, month, is_backdated = extract_month_from_text(original_text)

    # Check if this is a business payment mentioning a person (don't extract as person)
    business_person_keywords = ['gởi jacob', 'goi jacob', 'tiền jacob', 'tien jacob', 'jacob fee', 'fee jacob',
                                 'chị dương', 'chi duong', 'tiền dương', 'tien duong']
    is_business_payment = any(kw in text.lower() for kw in business_person_keywords)

    if is_business_payment:
        person = user_name  # Keep original user, don't extract from text
    else:
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
        'is_loan': is_loan_transaction(description)
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
    
    if is_loan:
        response += "\n💡 Track with `list debt`"
    
    if duplicate_warning:
        response += f"\n\n⚠️ *Warning:* You already logged {fmt(amount)} \"{description}\" today!"
        response += "\nDuplicate? Use `undo` to remove."
    else:
        personality = get_personality_response(category, category_data, amount, is_income_tx)
        if personality:
            response += f"\n{personality}"

    # Budget warning (only for expenses)
    if tx_data['type'] == 'Expense' and not tx_data.get('fixed_bill'):
        warning_type, warning_data = check_budget_warning(category, amount)

        if warning_type == 'over':
            response += f"\n\n🚨 *VƯỢT NGÂN SÁCH!* {category} vượt {fmt(warning_data['over'])}\n"
            response += f"• Ngân sách: {fmt(warning_data['budget'])}\n"
            response += f"• Đã chi: {fmt(warning_data['spent'])}\n"
            wisdom = get_wisdom('over_budget')
            if wisdom:
                response += f"\n{wisdom}"
        elif warning_type == 'warning':
            response += f"\n\n⚠️ *Chú ý nha!* Đã dùng {warning_data['percentage']:.0f}% ngân sách {category}\n"
            response += f"• Đã chi: {fmt(warning_data['spent'])} / {fmt(warning_data['budget'])}\n"
            response += f"• Còn lại: {fmt(warning_data['remaining'])}"
            wisdom = get_wisdom('spending')
            if wisdom:
                response += f"\n\n{wisdom}"
        elif warning_type == 'ok' and warning_data['budget'] > 0:
            response += f"\n\n✅ Còn {fmt(warning_data['remaining'])} cho {category} tháng này"

    # Income celebration with wisdom
    if tx_data['type'] == 'Income':
        if amount >= 5000000:  # ₩5M+ is big income
            response += f"\n\n{random.choice(CELEBRATION_MESSAGES['big_income'])}"
        elif amount >= 1000000:  # ₩1M+ gets celebration
            response += f"\n\n{random.choice(CELEBRATION_MESSAGES['income'])}"

        wisdom = get_wisdom('income')
        if wisdom:
            response += f"\n{wisdom}"

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
        if tx_type in ['Income', 'Expense', 'Fund Add', 'Fund Balance']:
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
        
        emoji = "💵" if tx['type'] == 'Income' else "🎯" if tx['type'] in ['Fund Add', 'Fund Balance'] else "💸"
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
        if row.get('Type') == 'Fund Balance':
            fund_name = row.get('Category', '')
            row_date = str(row.get('Date', ''))

            # Only update if this is newer or first occurrence
            if fund_name not in funds or row_date > funds[fund_name]['date']:
                funds[fund_name] = {
                    'amount': row.get('Amount', 0),
                    'date': row_date
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

    elif action_type == 'fund_update':
        sheet = get_transaction_sheet()
        if sheet:
            try:
                if data.get('was_new'):
                    # Delete the newly created row
                    sheet.delete_rows(data['row_index'])
                else:
                    # Restore old amount
                    sheet.update_cell(data['row_index'], 4, data['old_amount'])
                clear_undo_action(channel_id)
                return True, f"↩️ {data['fund_name']} restored to {fmt(data['old_amount'])}"
            except Exception as e:
                return False, str(e)
        return False, "Cannot connect to sheet"

    elif action_type == 'fund_apply':
        sheet = get_transaction_sheet()
        if sheet:
            try:
                # Delete all added rows (in reverse order to maintain indices)
                rows_to_delete = sorted(data['rows'], key=lambda x: x['row_index'], reverse=True)
                for row_info in rows_to_delete:
                    sheet.delete_rows(row_info['row_index'])
                clear_undo_action(channel_id)
                return True, f"↩️ Fund allocation undone ({len(rows_to_delete)} entries removed)"
            except Exception as e:
                return False, str(e)
        return False, "Cannot connect to sheet"

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

        # Command: Fund calculator
        elif text_lower in ['fund', 'quỹ', 'quy', 'tính quỹ', 'tinh quy']:
            sheet = get_transaction_sheet()
            if not sheet:
                slack_client.chat_postMessage(channel=channel, text="❌ Không thể kết nối sheet")
                return jsonify({'ok': True})

            now = datetime.now()
            current_month = now.strftime('%Y-%m-01')

            records = sheet.get_all_records()

            # Calculate income by person
            income_jacob = 0
            income_naomi = 0
            income_other = 0
            jacob_salary_amount = 0
            naomi_salary_amount = 0
            naomi_commission_amount = 0

            # Calculate business costs
            business_costs = 0
            ads_naomi_amount = 0
            jacob_fee_amount = 0
            chi_duong_amount = 0

            # Calculate joint expenses
            joint_expenses = 0

            for row in records:
                row_month = str(row.get('Month', ''))[:10]
                if row_month != current_month:
                    continue

                tx_type = row.get('Type', '')
                category = row.get('Category', '')
                person = row.get('Person', '')
                amount = row.get('Amount', 0) or 0
                description = str(row.get('Description', '')).lower()

                # Income tracking
                if tx_type == 'Income':
                    if person == 'Jacob':
                        income_jacob += amount
                        if 'salary' in description or 'lương' in description:
                            jacob_salary_amount += amount
                    elif person == 'Naomi':
                        income_naomi += amount
                        if 'salary' in description or 'lương' in description:
                            naomi_salary_amount += amount
                        if 'commission' in description or 'hoa hồng' in description:
                            naomi_commission_amount += amount
                    else:
                        income_other += amount

                # Business costs tracking
                if tx_type == 'Expense' and category == 'Business':
                    business_costs += amount
                    if 'ads' in description or 'quảng cáo' in description:
                        ads_naomi_amount += amount
                    if 'jacob' in description or 'gởi jacob' in description or 'fee' in description:
                        jacob_fee_amount += amount
                    if 'dương' in description or 'duong' in description:
                        chi_duong_amount += amount

                # Joint expenses tracking
                if tx_type == 'Expense' and person == 'Joint':
                    joint_expenses += amount

            total_income = income_jacob + income_naomi + income_other

            # Fixed expenses (set amount - user can update with command later)
            fixed_expenses = 3300000  # ₩3.3M default

            # Net pool calculation
            net_pool = total_income - fixed_expenses - business_costs - joint_expenses

            # Fund allocation (40/30/20/10)
            alloc_emergency = int(net_pool * 0.40)
            alloc_investment = int(net_pool * 0.30)
            alloc_planning = int(net_pool * 0.20)
            alloc_date = int(net_pool * 0.10)

            # Build response
            month_name = now.strftime('%B %Y')
            msg = f"📊 *Fund Calculator - {month_name}*\n\n"

            # Income section
            msg += "💵 *INCOME:*\n"
            if jacob_salary_amount > 0:
                msg += f"✅ Jacob Salary: {fmt(jacob_salary_amount)}\n"
            else:
                msg += f"❓ Jacob Salary: _chưa nhập_ → `jacob salary 2.8M`\n"

            if naomi_salary_amount > 0:
                msg += f"✅ Naomi Salary: {fmt(naomi_salary_amount)}\n"
            else:
                msg += f"❓ Naomi Salary: _chưa nhập_ → `naomi salary 2M`\n"

            if naomi_commission_amount > 0:
                msg += f"✅ Naomi Commission: {fmt(naomi_commission_amount)}\n"
            else:
                msg += f"❓ Naomi Commission: _chưa nhập_ → `naomi commission 5M`\n"

            if income_other > 0:
                msg += f"✅ Other: {fmt(income_other)}\n"

            msg += f"📝 *Total Income: {fmt(total_income)}*\n\n"

            # Fixed expenses
            msg += f"💸 *FIXED EXPENSES:* {fmt(fixed_expenses)}\n\n"

            # Business costs section
            msg += "💼 *BUSINESS COSTS:*\n"
            if ads_naomi_amount > 0:
                msg += f"✅ Ads Naomi: {fmt(ads_naomi_amount)}\n"
            else:
                msg += f"❓ Ads Naomi? → `50K ads naomi`\n"

            if jacob_fee_amount > 0:
                msg += f"✅ Jacob Fee: {fmt(jacob_fee_amount)}\n"
            else:
                msg += f"❓ Jacob Fee? → `800K gởi jacob`\n"

            if chi_duong_amount > 0:
                msg += f"✅ Chị Dương: {fmt(chi_duong_amount)}\n"
            else:
                msg += f"❓ Chị Dương? → `500K chị dương`\n"

            msg += f"📝 *Total Business: {fmt(business_costs)}*\n\n"

            # Joint expenses
            if joint_expenses > 0:
                msg += f"🛒 *JOINT EXPENSES:* {fmt(joint_expenses)}\n\n"

            # Net pool
            msg += "───────────────\n"
            msg += f"💰 *NET POOL: {fmt(net_pool)}*\n\n"

            # Suggested allocation
            msg += "*Suggested Allocation (40/30/20/10):*\n"
            msg += f"• 🎯 Emergency: {fmt(alloc_emergency)}\n"
            msg += f"• 📈 Investment: {fmt(alloc_investment)}\n"
            msg += f"• 🏠 Planning: {fmt(alloc_planning)}\n"
            msg += f"• 💕 Date: {fmt(alloc_date)}\n\n"

            # Actions
            msg += "✅ Apply suggested? → `fund apply`\n"
            msg += f"✏️ Custom amounts? → `fund apply {fmt(alloc_emergency)} {fmt(alloc_investment)} {fmt(alloc_planning)} {fmt(alloc_date)}`"

            # Store allocation for fund apply command
            store_undo_action(channel, 'fund_calc', {
                'emergency': alloc_emergency,
                'investment': alloc_investment,
                'planning': alloc_planning,
                'date': alloc_date
            })

            slack_client.chat_postMessage(channel=channel, text=msg)

        # Command: Fund apply
        elif text_lower.startswith('fund apply') or text_lower.startswith('áp dụng quỹ') or text_lower.startswith('ap dung quy'):
            sheet = get_transaction_sheet()
            if not sheet:
                slack_client.chat_postMessage(channel=channel, text="❌ Không thể kết nối sheet")
                return jsonify({'ok': True})

            now = datetime.now()

            # Check if custom amounts provided: "fund apply 2.5M 1.8M 1M 500K"
            # Remove the command prefix
            amounts_text = text_lower.replace('fund apply', '').replace('áp dụng quỹ', '').replace('ap dung quy', '').strip()

            custom_amounts = []
            if amounts_text:
                # Parse custom amounts
                parts = amounts_text.replace(',', ' ').split()
                for part in parts:
                    amt = parse_amount(part)
                    if amt:
                        custom_amounts.append(amt)

            if len(custom_amounts) == 4:
                # Use custom amounts
                alloc_emergency = custom_amounts[0]
                alloc_investment = custom_amounts[1]
                alloc_planning = custom_amounts[2]
                alloc_date = custom_amounts[3]
                is_custom = True
            else:
                # Get suggested amounts from last fund calculation
                action, error = get_undo_action(channel)
                if not action or action.get('type') != 'fund_calc':
                    slack_client.chat_postMessage(channel=channel, text="❓ Chạy `fund` trước để tính toán, hoặc nhập số tiền:\n`fund apply 2.5M 1.8M 1M 500K`\n(Emergency, Investment, Planning, Date)")
                    return jsonify({'ok': True})

                calc_data = action['data']
                alloc_emergency = calc_data['emergency']
                alloc_investment = calc_data['investment']
                alloc_planning = calc_data['planning']
                alloc_date = calc_data['date']
                is_custom = False

            # Validate - don't apply negative amounts
            if alloc_emergency < 0 or alloc_investment < 0 or alloc_planning < 0 or alloc_date < 0:
                slack_client.chat_postMessage(channel=channel, text="❌ Không thể áp dụng số âm. Kiểm tra lại income và expenses.")
                return jsonify({'ok': True})

            # Log each fund allocation as "Fund Add"
            fund_allocations = [
                ('Emergency Fund', '🎯', alloc_emergency),
                ('Investment Fund', '📈', alloc_investment),
                ('Planning Fund', '🏠', alloc_planning),
                ('Date Fund', '💕', alloc_date),
            ]

            # Get old Emergency Fund balance for milestone check
            old_funds = get_fund_status()
            old_emergency_balance = old_funds.get('Emergency Fund', {}).get('amount', 0)

            added_rows = []
            for fund_name, emoji, amount in fund_allocations:
                if amount > 0:
                    row_data = [
                        now.strftime('%Y-%m-%d'),
                        'Fund Add',
                        fund_name,
                        amount,
                        f'Monthly allocation - {now.strftime("%b %Y")}',
                        'Joint',
                        now.strftime('%Y-%m-01'),
                        'slack'
                    ]
                    sheet.append_row(row_data)

                    # Track for undo
                    all_values = sheet.get_all_values()
                    added_rows.append({
                        'row_index': len(all_values),
                        'fund_name': fund_name,
                        'amount': amount
                    })

            # Store for undo
            store_undo_action(channel, 'fund_apply', {'rows': added_rows})

            # Get updated fund balances
            funds = get_fund_status()

            # Build response
            msg = "✅ *Fund Allocation Applied!*\n\n"

            if is_custom:
                msg += "📝 Custom amounts:\n"
            else:
                msg += "📝 Suggested amounts (40/30/20/10):\n"

            total_allocated = 0
            for fund_name, emoji, amount in fund_allocations:
                if amount > 0:
                    new_balance = funds.get(fund_name, {}).get('amount', 0)
                    msg += f"{emoji} {fund_name}: +{fmt(amount)} → {fmt(new_balance)}\n"
                    total_allocated += amount

            msg += f"\n💰 Total allocated: {fmt(total_allocated)}\n"

            # Emergency fund progress
            emergency_balance = funds.get('Emergency Fund', {}).get('amount', 0)
            progress = (emergency_balance / 15000000) * 100
            msg += f"\n🎯 Emergency Fund: {progress:.1f}% → ₩15M"

            # Check for milestone
            milestone_msg = check_milestone('Emergency Fund', old_emergency_balance, emergency_balance)
            if milestone_msg:
                msg += f"\n\n{milestone_msg}"
                wisdom = get_wisdom('milestone')
                if wisdom:
                    msg += f"\n{wisdom}"
            elif progress >= 100:
                msg += "\n🎊 CONGRATULATIONS! Freedom achieved! 🎊"
            elif progress >= 75:
                msg += "\n🔥 Almost there! Keep going!"
            elif progress >= 50:
                msg += "\n💪 Halfway to freedom!"

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
            parts = text_lower.split()
            if len(parts) < 2 or not parts[1].isdigit():
                slack_client.chat_postMessage(channel=channel, text="❓ Usage: `paid 1` (mark loan #1 as paid)")
                return jsonify({'ok': True})
            
            loan_index = int(parts[1]) - 1
            success, result, undo_data = mark_loan_as_paid(loan_index, channel)
            
            if success:
                # Store for undo
                store_undo_action(channel, 'paid', undo_data)
                
                msg = f"✅ Paid: {fmt(result['amount'])} - {result['description']}\n"
                msg += f"💰 Logged as income: nhận lại/trả nợ"
                slack_client.chat_postMessage(channel=channel, text=msg)
            else:
                slack_client.chat_postMessage(channel=channel, text=f"❌ {result}")
        
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

        # Command: Settings
        elif text_lower in ['settings', 'cài đặt', 'cai dat']:
            spending = get_monthly_spending_by_category()
            now = datetime.now()
            month_name = now.strftime('%B %Y')

            msg = f"⚙️ *CÀI ĐẶT - {month_name}*\n\n"

            msg += "💰 *NGÂN SÁCH THÁNG:*\n"
            for category, budget in DEFAULT_BUDGETS.items():
                spent = spending.get(category, 0)
                percentage = (spent / budget * 100) if budget > 0 else 0

                if percentage >= 100:
                    status = "🚨"
                elif percentage >= 80:
                    status = "⚠️"
                else:
                    status = "✅"

                msg += f"{status} {category}: {fmt(spent)} / {fmt(budget)} ({percentage:.0f}%)\n"

            msg += f"\n🎭 *PERSONALITY:*\n"
            msg += f"• Tone: Vietnamese Mix 🇻🇳\n"
            msg += f"• Wisdom: 50% (balanced)\n"
            msg += f"• Celebrations: On 🎉\n"
            msg += f"• Warnings: On ⚠️\n"

            msg += f"\n✏️ Change budget: `set budget dining 300K`"

            slack_client.chat_postMessage(channel=channel, text=msg)

        # Command: Budgets quick view
        elif text_lower in ['budgets', 'ngân sách', 'ngan sach', 'budget']:
            spending = get_monthly_spending_by_category()

            msg = "💰 *NGÂN SÁCH THÁNG NÀY:*\n\n"

            total_budget = 0
            total_spent = 0

            for category, budget in DEFAULT_BUDGETS.items():
                spent = spending.get(category, 0)
                percentage = (spent / budget * 100) if budget > 0 else 0

                total_budget += budget
                total_spent += spent

                if percentage >= 100:
                    bar = "🔴"
                elif percentage >= 80:
                    bar = "🟡"
                else:
                    bar = "🟢"

                msg += f"{bar} {category}: {fmt(spent)} / {fmt(budget)}\n"

            msg += f"\n───────────────\n"
            msg += f"📊 Tổng: {fmt(total_spent)} / {fmt(total_budget)}\n"

            remaining_total = total_budget - total_spent
            if remaining_total > 0:
                msg += f"✅ Còn lại: {fmt(remaining_total)}"
            else:
                msg += f"🚨 Vượt: {fmt(abs(remaining_total))}"

            slack_client.chat_postMessage(channel=channel, text=msg)

        # Command: Set budget
        elif text_lower.startswith('set budget') or text_lower.startswith('đặt ngân sách'):
            # Parse: "set budget dining 300K" or "set budget Food & Dining 300000"
            parts = text.split()

            if len(parts) < 4:
                slack_client.chat_postMessage(channel=channel, text="❓ Cách dùng: `set budget dining 300K`\n\nCategories: dining, groceries, entertainment, shopping, transport, business, healthcare, gift")
                return jsonify({'ok': True})

            # Map short names to full category names
            category_map = {
                'dining': 'Food & Dining',
                'food': 'Food & Dining',
                'groceries': 'Groceries',
                'grocery': 'Groceries',
                'entertainment': 'Entertainment',
                'shopping': 'Shopping',
                'transport': 'Transport',
                'business': 'Business',
                'healthcare': 'Healthcare',
                'health': 'Healthcare',
                'gift': 'Gift',
            }

            category_input = parts[2].lower()
            category = category_map.get(category_input)

            if not category:
                slack_client.chat_postMessage(channel=channel, text=f"❓ Không tìm thấy category '{category_input}'\n\nCategories: dining, groceries, entertainment, shopping, transport, business, healthcare, gift")
                return jsonify({'ok': True})

            amount = parse_amount(parts[3])
            if not amount:
                slack_client.chat_postMessage(channel=channel, text=f"❓ Số tiền không hợp lệ: '{parts[3]}'\n\nVí dụ: `set budget dining 300K`")
                return jsonify({'ok': True})

            # Update budget (in memory)
            old_budget = DEFAULT_BUDGETS.get(category, 0)
            DEFAULT_BUDGETS[category] = amount

            msg = f"✅ Đã cập nhật ngân sách!\n\n"
            msg += f"📝 {category}: {fmt(old_budget)} → {fmt(amount)}\n"
            msg += f"\n💡 Xem tất cả: `budgets`"

            slack_client.chat_postMessage(channel=channel, text=msg)

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
• `list debt` - Outstanding loans
• `last 5` - Last 5 transactions

*🗑️ Delete:*
• `delete 1` or `delete 1,2,3` or `delete 1-5`
• `delete last` or `delete last 3`

*💰 Loans:*
• `list debt` - See all loans
• `paid 1` - Mark loan #1 as repaid

*✏️ Edit:*
• `edit 1 150K` - Change amount

*↩️ Undo (works for any last action):*
• `undo` - Undo last add/delete/edit/paid

*📊 Status:*
• `status` - Summary + funds
• `bills` - Fixed bills

*⚙️ Settings:*
• `settings` - Xem cài đặt
• `budgets` - Xem ngân sách
• `set budget dining 300K` - Đổi ngân sách"""
            slack_client.chat_postMessage(channel=channel, text=help_msg)
        
        # Command: Update fund balance (set total directly)
        elif text_lower.startswith('update fund') or text_lower.startswith('cập nhật quỹ') or text_lower.startswith('cap nhat quy'):
            fund_keywords = {
                'emergency': ('Emergency Fund', '🎯'),
                'khẩn cấp': ('Emergency Fund', '🎯'),
                'khan cap': ('Emergency Fund', '🎯'),
                'investment': ('Investment Fund', '📈'),
                'đầu tư': ('Investment Fund', '📈'),
                'dau tu': ('Investment Fund', '📈'),
                'planning': ('Planning Fund', '🏠'),
                'kế hoạch': ('Planning Fund', '🏠'),
                'ke hoach': ('Planning Fund', '🏠'),
                'date': ('Date Fund', '💕'),
                'hẹn hò': ('Date Fund', '💕'),
                'hen ho': ('Date Fund', '💕'),
            }

            # Find which fund mentioned
            fund_name = None
            fund_emoji = '💰'
            for keyword, (name, emoji) in fund_keywords.items():
                if keyword in text_lower:
                    fund_name = name
                    fund_emoji = emoji
                    break

            if not fund_name:
                slack_client.chat_postMessage(channel=channel, text="❓ Cách dùng:\n• `update fund emergency 8.7M`\n• `cập nhật quỹ khẩn cấp 8.7M`\n\nFunds: emergency, investment, planning, date")
                return jsonify({'ok': True})

            # Extract amount (this is the NEW TOTAL)
            amount, _ = extract_amount_from_text(text)

            if not amount:
                slack_client.chat_postMessage(channel=channel, text=f"❓ Thiếu số tiền. Ví dụ: `update fund emergency 8.7M`")
                return jsonify({'ok': True})

            # Get old balance
            funds = get_fund_status()
            old_balance = funds.get(fund_name, {}).get('amount', 0)

            # Always create NEW Fund Balance row (so it shows in list)
            sheet = get_transaction_sheet()
            if sheet:
                now = datetime.now()

                # Create new Fund Balance row
                row_data = [
                    now.strftime('%Y-%m-%d'),
                    'Fund Balance',
                    fund_name,
                    amount,
                    f'Update {fund_name}: {fmt(old_balance)} → {fmt(amount)}',
                    'Joint',
                    now.strftime('%Y-%m-01'),
                    'slack'
                ]
                sheet.append_row(row_data)
                all_values = sheet.get_all_values()
                old_data = {'row_index': len(all_values), 'old_amount': old_balance, 'fund_name': fund_name, 'was_new': True}

                # Store for undo
                store_undo_action(channel, 'fund_update', old_data)

                # Calculate change
                change = amount - old_balance
                if change >= 0:
                    change_str = f"+{fmt(change)}"
                else:
                    change_str = f"{fmt(change)}"

                # Progress for Emergency Fund
                progress_msg = ""
                if fund_name == 'Emergency Fund':
                    progress = (amount / 15000000) * 100
                    progress_msg = f"\n🎯 Tiến độ: {progress:.1f}% → ₩15M"

                msg = f"{fund_emoji} {fund_name} Updated!\n"
                msg += f"Số dư: {fmt(old_balance)} → {fmt(amount)} ({change_str}){progress_msg}"

                # Check for milestone
                milestone_msg = check_milestone(fund_name, old_balance, amount)
                if milestone_msg:
                    msg += f"\n\n{milestone_msg}"
                    wisdom = get_wisdom('milestone')
                    if wisdom:
                        msg += f"\n{wisdom}"

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

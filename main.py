import os
import telebot
import requests
import threading
import json
from flask import Flask
from io import BytesIO

# --- Configuration and Environment Variables ---

# Securely fetch your secret tokens and admin ID from Render's environment variables.
BOT_TOKEN = os.environ.get('BOT_TOKEN')
HF_TOKEN = os.environ.get('HF_TOKEN')
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID') # Your numeric Telegram User ID

# --- Persistent Storage Configuration for Render Disk ---

# Render Disks are mounted at a specific path. We'll store our user data here.
# Make sure the Mount Path in Render settings matches this.
DATA_DIR = "/var/data"
USER_DATA_FILE = os.path.join(DATA_DIR, "users.json")

# --- Hugging Face API Configuration ---
API_URL = "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-inpainting"
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}

# --- Bot and Flask Initialization ---
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# --- User Data Management ---

# A lock is crucial to prevent race conditions when multiple users access the JSON file at once.
user_data_lock = threading.Lock()

def load_user_data():
    """Loads the user database from the JSON file."""
    with user_data_lock:
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR) # Create the directory if it doesn't exist
        try:
            with open(USER_DATA_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

def save_user_data(data):
    """Saves the user database to the JSON file."""
    with user_data_lock:
        with open(USER_DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)

# Load data once at startup
user_data = load_user_data()


# --- Core AI and Bot Logic ---

def query_inpainting_api(image_bytes: bytes) -> bytes | None:
    """Sends image to the Hugging Face API for processing."""
    try:
        response = requests.post(API_URL, headers=HEADERS, data=image_bytes, timeout=45)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        print(f"Error calling Hugging Face API: {e}")
        return None

# --- Flask Web Server for Render Hosting ---

@app.route('/')
def index():
    """Health-check endpoint for Render."""
    return "Telegram Inpainting Bot with Credits is alive!"

def run_flask():
    """Runs the Flask web server."""
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


# --- Telegram Bot Handlers ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Handles new users, referrals, and displays user status."""
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name
    
    with user_data_lock:
        # Check for referral
        try:
            # Command might be "/start referrer_12345"
            referrer_id = message.text.split()[1].replace('referrer_', '')
            if referrer_id != user_id and user_id not in user_data:
                # Give the referrer a credit if they exist
                if referrer_id in user_data:
                    user_data[referrer_id]['credits'] += 1
                    try:
                        bot.send_message(referrer_id, f"🎉 Success! {user_name} joined using your link. You've earned 1 free image credit!")
                    except Exception as e:
                        print(f"Could not notify referrer {referrer_id}: {e}")
        except IndexError:
            referrer_id = None # No referral code was provided

        # Add new user to the database if they don't exist
        if user_id not in user_data:
            user_data[user_id] = {'credits': 1, 'is_premium': False}
            if referrer_id:
                bot.send_message(user_id, "Welcome! Since you joined via a referral, you start with 1 free image credit.")
            else:
                 bot.send_message(user_id, "Welcome! You have 1 free image credit to start.")

        save_user_data(user_data) # Save changes
    
    # Generate referral link
    bot_info = bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=referrer_{user_id}"

    # Get user status
    status = user_data.get(user_id, {'credits': 1, 'is_premium': False})
    credits = status['credits']
    is_premium = status['is_premium']
    
    if is_premium:
        status_message = "✅ You are a **Premium** user with unlimited processing!"
    else:
        status_message = f"🖼️ You have **{credits}** free image credit(s) remaining."

    welcome_text = (
        f"👋 Hello, {user_name}!\n\n{status_message}\n\n"
        "To process an image, simply send it to me.\n\n"
        f"To earn more free credits, share your referral link:\n`{referral_link}`"
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """Processes photos, checking user credits first."""
    user_id = str(message.from_user.id)
    
    with user_data_lock:
        # Ensure user exists in the database
        if user_id not in user_data:
            user_data[user_id] = {'credits': 1, 'is_premium': False}
        
        status = user_data[user_id]
        can_process = status['is_premium'] or status['credits'] > 0

    if not can_process:
        bot_info = bot.get_me()
        referral_link = f"https://t.me/{bot_info.username}?start=referrer_{user_id}"
        
        limit_message = (
            "🚫 **You've used all your free credits!**\n\n"
            "To process more images, you can:\n\n"
            "1️⃣ **Upgrade to Premium** for unlimited access. Contact the admin for details.\n"
            f"   Admin: @{ADMIN_USER_ID} (if admin has a username) \n\n"
            f"2️⃣ **Refer a friend!** You get 1 free credit for every friend who joins using your link:\n`{referral_link}`"
        )
        bot.reply_to(message, limit_message, parse_mode='Markdown')
        return

    # If the user can process, proceed...
    try:
        reply_msg = bot.reply_to(message, "⏳ Processing your image... this may take a moment.")
        
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        edited_image_bytes = query_inpainting_api(downloaded_file)

        if edited_image_bytes:
            bot.send_photo(message.chat.id, photo=BytesIO(edited_image_bytes), caption="✅ Here is your edited image!")
            bot.delete_message(message.chat.id, reply_msg.message_id)
            
            # Decrement credit count if not premium
            with user_data_lock:
                if not user_data[user_id]['is_premium']:
                    user_data[user_id]['credits'] -= 1
                save_user_data(user_data)
        else:
            bot.edit_message_text("❌ Sorry, the AI model failed to process the image. Please try again later. No credit was used.", chat_id=message.chat.id, message_id=reply_msg.message_id)
    except Exception as e:
        print(f"Error in handle_photo: {e}")
        bot.reply_to(message, "An unexpected error occurred. Please try again.")

# --- Main Execution Block ---
if __name__ == "__main__":
    print("Starting Flask server in a new thread...")
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    print("Starting Telegram Bot polling...")
    bot.polling(none_stop=True)

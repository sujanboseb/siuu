import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS  # CORS support

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Environment variables
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN")
PORT = int(os.getenv("PORT", 5000))
FASTAPI_URL = os.getenv("FASTAPI_URL")

# Store processed message IDs to avoid duplicate replies
processed_message_ids = set()

# Handle incoming WhatsApp messages
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("Incoming webhook message:", json.dumps(data, indent=2))

    message = data.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {}).get('messages', [{}])[0]
    message_id = message.get('id')

    # Check if the message is text and has not been processed yet
    if message.get('type') == "text" and message_id and message_id not in processed_message_ids:
        sender_phone_number = message.get('from')  # Extract sender's phone number
        business_phone_number_id = data.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {}).get('metadata', {}).get('phone_number_id')

        try:
            # Mark the message as being processed to avoid duplicates
            processed_message_ids.add(message_id)

            # Forward the message and phone number to the FastAPI server
            fastapi_response = forward_message_to_fastapi(message['text']['body'], sender_phone_number)

            # Send a reply message to the user
            send_reply_to_user(business_phone_number_id, sender_phone_number, fastapi_response, message_id)

            # Mark the incoming message as read
            mark_message_as_read(business_phone_number_id, message_id)
            print("Message sent and marked as read successfully.")
        except Exception as error:
            print("Error processing message:", str(error))
            # Optionally, remove the messageId from the set in case of an error
            processed_message_ids.discard(message_id)

    return '', 200  # Acknowledge receipt of the message

# Function to forward the message to the FastAPI server
def forward_message_to_fastapi(text, phone_number):
    response = requests.post(FASTAPI_URL, json={
        "text": text,
        "phone_number": phone_number
    })
    return response.json()  # Return the response data for further processing

# Function to send a reply to the user
def send_reply_to_user(business_phone_number_id, phone_number, fastapi_response, message_id):
    # Remove double quotes from the fastApiResponse if any
    cleaned_response = json.dumps(fastapi_response).replace('"', '')

    response = requests.post(
        f"https://graph.facebook.com/v20.0/{business_phone_number_id}/messages",
        json={
            "messaging_product": "whatsapp",
            "to": phone_number,
            "text": {"body": cleaned_response},
            "context": {"message_id": message_id}
        },
        headers={
            "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    return response.json()

# Function to mark the incoming message as read
def mark_message_as_read(business_phone_number_id, message_id):
    requests.post(
        f"https://graph.facebook.com/v20.0/{business_phone_number_id}/messages",
        json={
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        },
        headers={
            "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
            "Content-Type": "application/json"
        }
    )

# Verify the webhook during setup
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        print("Webhook verified successfully!")
        return challenge, 200
    else:
        return '', 403  # Forbidden

# Root endpoint
@app.route("/", methods=["GET"])
def home():
    return "<pre>Nothing to see here. Checkout README.md to start.</pre>"

# Start the server (only for development, in production use gunicorn)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS  # CORS support
import pymongo
import re
from datetime import datetime, timedelta
import pytz
from dateutil import parser
import random
import string

# this is fn to handle message
mongo_uri = 'mongodb+srv://sujanboseplant04:XY1LyC86iRTjEgba@cluster0.mrenu.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0'
client = pymongo.MongoClient(mongo_uri)
db = client['sujan']
conversation_state_collection = db['s']
meeting_booking_collection = db['meeting_booking']
cab_booking_collection=db['cab_booking']
local_tz='Asia/Kolkata'
# Predefined hall names
hall_names_with_webex = ["New York", "Mumbai", "Huston", "Amsterdam", "Delhi", "Tokyo", "Chicago"]
small_halls = ["0a", "0b", "0c", "1a", "1b", "1c", "2a", "2b", "2c"]


# Load environment variables
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Environment variables
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN")
fastapi_url = os.getenv('FASTAPI_URL')
PORT = int(os.getenv("PORT", 5000))


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

            # Simulate a request to /handle-message endpoint by sending data internally
            with app.test_request_context('/handle-message', method='POST', json={"text": message['text']['body'], "phone_number": sender_phone_number}):
                response = handle_message()

            # Send the response to the user via WhatsApp
            send_reply_to_user(business_phone_number_id, sender_phone_number, response.get_json(), message_id)

            # Mark the incoming message as read
            mark_message_as_read(business_phone_number_id, message_id)
            print("Message sent and marked as read successfully.")
        except Exception as error:
            print("Error processing message:", str(error))
            # Optionally, remove the messageId from the set in case of an error
            processed_message_ids.discard(message_id)

    return '', 200  # Acknowledge receipt of the message





# Function to send a reply to the user
def send_reply_to_user(business_phone_number_id, phone_number, response_data, message_id):
    # Remove double quotes from the response_data if any (but ensure it's JSON)
    if isinstance(response_data, str):
        cleaned_response = response_data.replace('"', '')
    else:
        cleaned_response = json.dumps(response_data)

    response = requests.post(
        f"https://graph.facebook.com/v20.0/{business_phone_number_id}/messages",
        json={
            "messaging_product": "whatsapp",
            "to": phone_number,
            "text": {"body": cleaned_response},  # Send the cleaned response
            "context": {"message_id": message_id}  # Include the original message ID for context
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


@app.route('/handle-message', methods=['POST'])
def handle_message():
    data = request.json
    text = data.get('text', '').lower()
    phone_number = data.get('phone_number')

    print("Received data in Flask:", data)

    if text == 'stop':
        # Remove the existing conversation state for this phone number
        conversation_state_collection.delete_one({"phone_number": phone_number})
        print(f"Conversation state for {phone_number} has been removed.")

        # Notify the user that the conversation has been reset
        return jsonify("Old conversation state has been removed.")

    # Check for existing conversation state
    conversation_state = conversation_state_collection.find_one({"phone_number": phone_number})

    if conversation_state:
        # Continue the existing conversation
        return continue_conversation(text, phone_number, conversation_state)
    else:
        # Call the external prediction service if there's no ongoing conversation
        try:
            predict_response = requests.post('https://5360-35-237-136-225.ngrok-free.app/predict', json={"text": text})
            predict_response.raise_for_status()
            intent_data = parse_predict_response(predict_response.text)
            print("Parsed response from prediction service:", intent_data)
            intent = intent_data.get('intent')
            if intent == 'meeting_booking':
                # Check if required entities are already present
                meeting_date = intent_data.get('meeting_date')
                starting_time = intent_data.get('starting_time')
                ending_time = intent_data.get('ending_time')
                hall_name = intent_data.get('hall_name')

                # If any entity is missing, ask for it
                if not hall_name:
                    return ask_for_hall_name(phone_number, intent_data)
                elif not meeting_date:
                    return ask_for_entity(phone_number, 'meeting_date', intent_data)
                elif not starting_time:
                    return ask_for_entity(phone_number, 'starting_time', intent_data)
                elif not ending_time:
                    return ask_for_entity(phone_number, 'ending_time', intent_data)
                else:
                    # If all entities are present, check for conflicts and complete the booking
                    return check_for_conflicts_and_book(phone_number, hall_name, meeting_date, starting_time, ending_time)

            elif intent == 'meeting_cancelling':
                # Check if meeting_id is provided
                meeting_booking_id = intent_data.get('meeting_booking_id')

                if not meeting_booking_id:
                    return ask_for_entity(phone_number, 'meeting_booking_id', intent_data)
                else:
                    # Proceed to ask for meeting_id if not already provided
                    return meeting_cancelling_id(phone_number, meeting_booking_id)

            if intent == 'Greetings':
              # Respond with the message for meeting and cab management
              greeting_message = (
                  "1.This number is for meeting and cab management.`"
                  "2.You can check  your meetings from the past dates.` "
                  "3.Please provide the ** meeting date ** in **'dd/mm/yyyy'** format and the ** time **  in **'hh:mm AM/PM'** format.`"
                  "4.if the text has been **STOP** means then u can satrt new conversation ok `"
              )
              return jsonify(greeting_message)

            elif intent == 'cab_cancelling':
                # Check if meeting_id is provided
                cab_booking_id = intent_data.get('cab_booking_id')

                if not cab_booking_id:
                    return ask_for_entity(phone_number, 'cab_booking_id', intent_data)
                else:
                    # Proceed to ask for meeting_id if not already provided
                    return cab_cancelling_id(phone_number, cab_booking_id)

            elif intent == 'list_meetings_booked':
                # Fetch bookings for the user based on their phone number
                print("Fetching bookings for phone number:", phone_number)
                meeting_date = intent_data.get('meeting_date')
                if not meeting_date:
                    return ask_for_entity(phone_number, 'meeting_date', intent_data)

                return handle_meeting_booking_stats(phone_number,meeting_date)

            elif intent == 'list_cabs_booked':

                # Fetch bookings for the user based on their phone number
                print("Fetching bookings for phone number:", phone_number)
                meeting_date = intent_data.get('meeting_date')
                if not meeting_date:
                    return ask_for_entity(phone_number, 'meeting_date', intent_data)
                return handle_cab_booking_stats(phone_number,meeting_date)


            elif intent =="cab_booking":
              meeting_date = intent_data.get('meeting_date')
              starting_time = intent_data.get('starting_time')

              # Ask for missing entities in sequence
              if not meeting_date:
                  return ask_for_entity(phone_number, 'meeting_date', intent_data)
              elif not starting_time:
                  return ask_for_entity(phone_number, 'starting_time', intent_data)
                  # If all entities are present, proceed with booking
              return handle_cab_selection(phone_number, starting_time, meeting_date)

            else:
                return jsonify("Unhandled intent. Please provide more information.")
        except requests.exceptions.RequestException as e:
            print(f"Error calling prediction service: {e}")
            return jsonify( "Failed to get intent from prediction service"), 500

    return jsonify( "Processed successfully")


def handle_meeting_booking_stats(phone_number, meeting_date):
    # Parse the provided meeting date string into a date object
    meeting_date_obj = datetime.strptime(meeting_date, '%d/%m/%Y').date()

    # Calculate the date range: 7 days before the provided meeting date
    start_date = (meeting_date_obj - timedelta(days=7)).strftime('%d/%m/%Y')
    end_date = (meeting_date_obj - timedelta(days=1)).strftime('%d/%m/%Y')  # Exclude the meeting date itself

    # Debug: Print the start and end date being used for the query
    print(f"Querying for bookings between {start_date} and {end_date}")

    # Fetch bookings for the provided phone number within the date range
    meeting_bookings = list(meeting_booking_collection.find({
        "phone_number": phone_number,
        "meeting_date": {
            "$gte": start_date,  # Start date (7 days before)
            "$lte": end_date     # End date (1 day before the provided meeting date)
        }
    }))

    # Debug: Print the raw query result
    print(f"Raw meeting bookings result: {meeting_bookings}")

    # If no bookings are found, return a message indicating no bookings
    if len(meeting_bookings) == 0:
        return jsonify("No meeting bookings found for your phone number in the past 7 days.")

    # Create a list to store formatted booking information
    booking_list = []

    # Iterate through the bookings and format the details
    for booking in meeting_bookings:
        booking_info = (
            f"*Booking ID:* {booking.get('bookings_id', 'N/A')}  "
            f"*Meeting Date:* {booking.get('meeting_date', 'N/A')}  "
            f"*Starting Time:* {booking.get('starting_time', 'N/A')}  "
            f"*Ending Time:* {booking.get('ending_time', 'N/A')}  "
            f"*Hall Name:* {booking.get('hall_name', 'N/A')}  "
        )
        booking_list.append(booking_info)

    # Debug: Print the final booking list
    print(f"Final booking list: {booking_list}")

    # Join the list of bookings with a visual separator
    response_message = "------------------------  ".join(booking_list)

    # Remove the conversation state for the given phone number
    conversation_state_collection.delete_one({"phone_number": phone_number})

    # Return the formatted booking details as a response
    return jsonify(response_message)






def handle_cab_booking_stats(phone_number, meeting_date):
    # Parse the provided meeting date string into a date object
    meeting_date_obj = datetime.strptime(meeting_date, '%d/%m/%Y').date()

    # Calculate the date range: 7 days before the provided meeting date
    start_date = meeting_date_obj - timedelta(days=7)
    end_date = meeting_date_obj - timedelta(days=1)  # Exclude the meeting date itself

    # Fetch cab bookings for the provided phone number within the date range
    cab_bookings = list(cab_booking_collection.find({
        "phone_number": phone_number,
        "meeting_date": {
            "$gte": start_date.strftime('%d/%m/%Y'),  # Start date (7 days before)
            "$lte": end_date.strftime('%d/%m/%Y')     # End date (1 day before the provided meeting date)
        }
    }))

    # If no bookings are found, return a message indicating no bookings
    if len(cab_bookings) == 0:
        return jsonify("No cab bookings found for your phone number in the past 7 days.")

    # Create a list to store formatted booking information
    booking_list = []

    # Iterate through the cab bookings and format the details
    for booking in cab_bookings:
        booking_info = (
            f"*Booking ID:* {booking.get('booking_id', 'N/A')}  "
            f"*Meeting Date:* {booking.get('meeting_date', 'N/A')}  "
            f"*Drop-off Point:* {booking.get('dropping_point', 'N/A')}  "
            f"*Cab Name:* {booking.get('cab_name', 'N/A')}  "
            f"*Starting Time:* {booking.get('starting_time', 'N/A')}  "
        )
        booking_list.append(booking_info)

    # Join the list of bookings with a visual separator
    response_message = "------------------------  ".join(booking_list)

    # Remove the conversation state for the given phone number
    conversation_state_collection.delete_one({"phone_number": phone_number})

    # Return the formatted cab booking details as a response
    return jsonify(response_message)


def is_valid_time_for_cabs(starting_time):
    # Define allowed times for the cabs
    allowed_times = ["18:30", "19:30"]
    return starting_time.strftime("%H:%M") in allowed_times

def ask_user_to_wait_or_exit(phone_number, message):
    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": {"state": "waiting_or_exit"}},
        upsert=True
    )
    return jsonify(message + " Please choose either '1) Wait for cab 2 ' or '2) Exit'. or please enter the option values")




def parse_predict_response(response_text):
    response_text = response_text.strip()[1:-1]  # Remove surrounding braces
    result = {}
    pairs = response_text.split(',')
    for pair in pairs:
        key, value = pair.split('=', 1)
        key = key.strip().replace('"', '').strip('[]')
        value = value.strip().replace('"', '').strip('[]')
        result[key] = value
    return result

def meeting_cancelling_id(phone_number, meeting_booking_id):
    # Ensure cab_booking_id is provided and is a string
    if not meeting_booking_id:
        # Update conversation state to ask for cab_booking_id
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "asking_meeting_booking_id"  # Update state to ask for cab booking ID
            }},
            upsert=True
        )
        return jsonify("Please provide the meeting booking ID for cancellation.")



    # Convert the cab_booking_id to uppercase
    meeting_booking_id = meeting_booking_id.upper()

    # Search for the cab booking in the cab_booking_collection using the provided (uppercase) cab_booking_id
    meeting_booking = meeting_booking_collection.find_one({"bookings_id": meeting_booking_id})

    if not meeting_booking:
        # If the cab booking ID does not exist, inform the user and ask for the correct ID again
        return jsonify( f"Meeting booking ID {meeting_booking_id} not found. Please provide a valid meeting booking ID.")

    # Check if the booking date is in the past
    meeting_date = meeting_booking.get('meeting_date')
    if meeting_date:
        # Convert the meeting_date to a datetime object for comparison
        meeting_date_obj = datetime.strptime(meeting_date, "%d/%m/%Y")

        # Get the current time in Indian Standard Time (IST)
        ist_timezone = pytz.timezone('Asia/Kolkata')
        today_ist = datetime.now(ist_timezone)

        # Check if the cab ride is in the past (compare only dates)
        if meeting_date_obj.date() < today_ist.date():
            return jsonify(f"hall booking for booking ID {meeting_booking_id} has already ended on {meeting_date}. You cannot cancel it now.")

    # If the meeting is not over, proceed with the cancellation
    if 'bookings_id' in meeting_booking and meeting_booking['bookings_id']:
        # Remove the 'bookings_id' attribute from the meeting_booking document
        meeting_booking_collection.update_one(
            {"bookings_id": meeting_booking_id},
            {"$unset": {"booking_id": ""}}
        )

        # After successfully cancelling the booking, check if any state is tied to this phone number
        conversation_state = conversation_state_collection.find_one({"phone_number": phone_number})

        # If there is any conversation state, delete the entire document from the collection
        if conversation_state:
            conversation_state_collection.delete_one({"phone_number": phone_number})
            print(f"Deleted conversation state for phone number {phone_number} after cancelling the meeting booking.")

        # Inform the user that the cab booking was successfully cancelled
        return jsonify(f"meeting booking with ID {meeting_booking_id} has been successfully cancelled and the booking ID has been removed.")
    else:
        # If no 'booking_id' exists, inform the user that there's nothing to cancel
        return jsonify( f"meeting booking with ID {meeting_booking_id} does not have an active booking to cancel.")

from datetime import datetime
import pytz

def cab_cancelling_id(phone_number, cab_booking_id):
    # Ensure cab_booking_id is provided
    if not cab_booking_id:
        # Update conversation state to ask for cab_booking_id
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "asking_cab_booking_id"  # Update state to ask for cab booking ID
            }},
            upsert=True
        )
        return jsonify("Please provide the cab booking ID for cancellation.")

    # Convert the cab_booking_id to uppercase for consistency
    cab_booking_id = cab_booking_id.upper()

    # Search for the cab booking in the cab_booking_collection using the provided cab_booking_id
    cab_booking = cab_booking_collection.find_one({"booking_id": cab_booking_id})

    if not cab_booking:
        # If the cab booking ID does not exist, remove the invalid cab_booking_id from the conversation state
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$unset": {"cab_booking_id": ""},  # Remove invalid cab_booking_id
             "$set": {"state": "asking_cab_booking_id"}  # Update state to ask for valid cab booking ID
            }
        )
        return jsonify(f"Cab booking ID {cab_booking_id} not found. Please provide a valid cab booking ID.")

    # Check if the booking date is in the past
    meeting_date = cab_booking.get('meeting_date')
    if meeting_date:
        # Convert the meeting_date to a datetime object for comparison
        meeting_date_obj = datetime.strptime(meeting_date, "%d/%m/%Y")

        # Get the current time in Indian Standard Time (IST)
        ist_timezone = pytz.timezone('Asia/Kolkata')
        today_ist = datetime.now(ist_timezone)

        # Check if the cab ride is in the past (compare only dates)
        if meeting_date_obj.date() < today_ist.date():
            return jsonify(f"Cab ride for booking ID {cab_booking_id} has already ended on {meeting_date}. You cannot cancel it now.")

    # If the cab ride is not over, proceed with the cancellation
    if 'booking_id' in cab_booking and cab_booking['booking_id']:
        # Remove the 'booking_id' attribute from the cab_booking document
        cab_booking_collection.update_one(
            {"booking_id": cab_booking_id},
            {"$unset": {"booking_id": ""}}
        )

        # After successfully cancelling the booking, check if any state is tied to this phone number
        conversation_state = conversation_state_collection.find_one({"phone_number": phone_number})

        # If there is any conversation state, delete the entire document from the collection
        if conversation_state:
            conversation_state_collection.delete_one({"phone_number": phone_number})
            print(f"Deleted conversation state for phone number {phone_number} after cancelling the cab booking.")

        # Inform the user that the cab booking was successfully cancelled
        return jsonify(f"Cab booking with ID {cab_booking_id} has been successfully cancelled and the booking ID has been removed.")
    else:
        # If no 'booking_id' exists, inform the user that there's nothing to cancel
        return jsonify(f"Cab booking with ID {cab_booking_id} does not have an active booking to cancel.")




def extract_dates(sentence):
    date_pattern = r'\b(?:\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{2,4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b'
    matches = re.findall(date_pattern, sentence)
    return matches

def convert_dates(dates):
    converted_dates = []
    for date_str in dates:
        try:
            # Enforcing day-first interpretation of dates
            date_obj = parser.parse(date_str, dayfirst=True)
            converted_dates.append(date_obj.strftime('%d/%m/%Y'))
        except ValueError:
            continue
    return converted_dates

def ask_for_cab_name(phone_number, intent_data=None):
    # Update the conversation state to "asking_cab_name"
    update_data = {"state": "asking_cab_name"}
    if intent_data:
        update_data.update(intent_data)

    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": update_data},
        upsert=True
    )

    # Available cabs and their routes
    available_cabs = {
        'Cab1': 'Chainsys Company, Elcot Main Gate, Madurai Kamaraj College',
        'Cab2': 'Chainsys Company, Elcot Main Gate, Madurai Kamaraj College, Nagamalai Puthukottai, Achampathu, Kalavasal'
    }

    # Create a list of messages where each cab and its route are displayed on separate lines
    response_message = [
        "Please choose from the available cabs:",
        "Cab1: Chainsys Company, Elcot Main Gate, Madurai Kamaraj College",
        "Cab2: Chainsys Company, Elcot Main Gate, Madurai Kamaraj College, Nagamalai Puthukottai, Achampathu, Kalavasal",
        "Enter the cab name."
    ]

    # Return the message in list format
    return jsonify({
         response_message
    })

def ask_for_batch_name(phone_number, intent_data=None):
    # Update the conversation state to "asking_batch_name"
    update_data = {"state": "asking_batch_name"}
    if intent_data:
        update_data.update(intent_data)

    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": update_data},
        upsert=True
    )

    # Available batches and their times
    available_batches = {
        'Batch1': '7:00 PM to 7:30 PM',
        'Batch2': '7:30 PM to 8:30 PM'
    }

    # Create a list of messages where each batch and its time are displayed on separate lines
    response_message = [
        "Please choose from the available batches:",
        *[f"{batch}: {time}" for batch, time in available_batches.items()],
        "Enter the batch name."
    ]

    # Return the message in list format
    return jsonify(response_message)




def ask_for_hall_name(phone_number, intent_data=None):
    update_data = {"state": "asking_hall_name"}
    if intent_data:
        update_data.update(intent_data)
    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": update_data},
        upsert=True
    )
    available_halls = ", ".join(hall_names_with_webex + small_halls)
    return jsonify( f"Please provide the hall name. Available halls are: {available_halls}.")





def extract_times(input_text):
    # Time pattern to match formats like 3pm, 3:00pm, 15:00, 5:30pm, etc.
    time_pattern = r'\b\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.|AM|PM|A\.M\.|P\.M\.)?|\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.|AM|PM|A\.M\.|P\.M\.)\b'

    # Find all time matches in the input text
    matches = re.findall(time_pattern, input_text, re.IGNORECASE)

    # Check if there are multiple times or no times found
    if len(matches) > 1:
        return None, "More than one time provided. Please provide a single time."
    if len(matches) == 0:
        return None, "No valid time found. Please provide a valid time."

    return matches[0], None


# Updated function to convert to 24-hour format and validate if it’s in HH:MM format
def convert_to_24_hour_format(time_str):
    if not time_str:  # Add safeguard for None input
        return None
    try:
        # Try to parse formats like '3pm' or '5:30pm'
        return datetime.strptime(time_str, "%I:%M%p").strftime("%H:%M")
    except ValueError:
        try:
            # Try parsing formats like '3pm'
            return datetime.strptime(time_str, "%I%p").strftime("%H:%M")
        except ValueError:
            return None  # Return None if the time is invalid


def ask_for_entity(phone_number, entity, intent_data=None):
    # Initialize the update data for state tracking
    update_data = {"state": f"asking_{entity}"}

    # If intent_data is provided, merge it with the update data
    if intent_data:
        update_data.update(intent_data)

    # Update the conversation state with the new entity being requested
    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": update_data},
        upsert=True
    )

    # Conditional checks for specific entities
    if entity == "meeting_date":
        # Special message for meeting date format
        return jsonify("Please provide the meeting date in **dd/mm/yyyy** format.")

    elif entity in ["starting_time", "ending_time"]:
        # Special message for time format (e.g., 3pm/2:15pm)
        return jsonify(f"Please provide the {entity.replace('_', ' ')} in **3pm/2:15pm** format.")

    # Default message for other entities
    return jsonify(f"Please provide the {entity.replace('_', ' ')}.")


def generate_unique_id(existing_ids):
    while True:
        # Generate a random 6-digit number
        random_number = ''.join(random.choices(string.digits, k=6))
        unique_id = f'C{random_number}'
        if unique_id not in existing_ids:
            return unique_id

def generate_unique_ids(existing_ids):
    while True:
        # Generate a random 6-digit number
        random_number = ''.join(random.choices(string.digits, k=6))
        unique_id = f'M{random_number}'
        if unique_id not in existing_ids:
            return unique_id

from datetime import datetime, time
from flask import jsonify





def handle_cab_selection(phone_number, starting_time, meeting_date):
    try:
        # Check if there's already a cab booking for the same meeting date and phone number
        existing_booking = cab_booking_collection.find_one({
            "phone_number": phone_number,
            "meeting_date": meeting_date
        })

        if existing_booking:
            booking_id = existing_booking.get("booking_id")  # Assuming booking ID is the document's _id field
            drop_off_point = existing_booking.get("dropping_point", "not specified")
            print(f"Booking ID: {booking_id}, Drop-off Point: {drop_off_point}")
            starting_times=existing_booking.get("starting_time","not specified")
            print(f"Starting Time: {starting_time}")

            # Inform the user that they already have a booking
            message = (f"You already have a cab booked on {meeting_date} "
                       f"with Booking ID: **{booking_id}**, drop-off point: **{drop_off_point}**. time:**{starting_times} "
                       "Please enter the one of the following option number or its value or its highlighted word:"
                       "**1)**  **Re-enter** the details starting from the meeting date"
                       "**2)**  **Exit**")

            # Update the state to asking for cab options
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"state": "asking_cab_validation"}},
                upsert=True
            )

            return jsonify(message)

        # If no existing booking, proceed with asking for drop-off point
        points = ["Elcot Main Gate", "Madurai Kamaraj College", "Nagamalai Puthukottai", "Achampathu", "Kalavasal"]
        bold_points = [f"**{point}**" for point in points]
        point_message = "Please enter your **drop-off point** from the following: " + ", ".join(bold_points)

        # Update conversation state to ask for drop-off point
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "asking_dropoff_point",
                "starting_time": starting_time,
                "meeting_date": meeting_date,
                "points": points
            }},
            upsert=True
        )

        return jsonify(point_message)

    except Exception as e:
        # Handle any potential errors
        return jsonify(f"An error occurred: {str(e)}")


# The generate_unique_id function you provided
def generate_unique_id(existing_ids):
    while True:
        # Generate a random 6-digit number
        random_number = ''.join(random.choices(string.digits, k=6))
        unique_id = f'C{random_number}'
        if unique_id not in existing_ids:
            return unique_id









def delete_conversation_state(phone_number):
    """
    Deletes the conversation state for the given phone number.
    """
    conversation_state_collection.delete_one({"phone_number": phone_number})
    return jsonify({"message": "Conversation state cleared successfully."})

def continue_conversation(text, phone_number, conversation_state):
    state = conversation_state.get('state')

    # Log the current state and conversation data
    print(f"Continuing conversation for phone number: {phone_number}")
    print(f"Current state: {state}")
    print(f"Conversation state data: {conversation_state}")

    if state == 'asking_hall_name':
        hall_name = text.title().strip()  # Normalize input
        
        # Split the hall names provided by the user by commas or other delimiters
        hall_names_provided = re.split(r'[,;\s]+', hall_name)  # Split by commas, semicolons, spaces, etc.
        
        # Remove empty strings from the list in case of extra spaces
        hall_names_provided = list(filter(None, hall_names_provided))
    
        # Check if more than one hall name is provided
        if len(hall_names_provided) > 1:
            return jsonify("Multiple hall names detected. Please enter only one hall name.")
    
        # Get the first and only hall name (since we've ensured only one is provided)
        hall_name = hall_names_provided[0]
        print(f"Received hall name: {hall_name}")
    
        # Check if the hall name is valid
        if hall_name not in hall_names_with_webex + small_halls:
            return jsonify("Invalid hall name. Please choose from the available options.")
    
        # Update the conversation state with the provided hall name
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"hall_name": hall_name}}
        )
        meeting_date = conversation_state.get('meeting_date')
        starting_time = conversation_state.get('starting_time')
        ending_time = conversation_state.get('ending_time')

        if not meeting_date:
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"state": "asking_meeting_date"}}
            )
            print("Updated state to 'asking_meeting_date'")
            return jsonify("Please provide the meeting date in **dd/mm/yyyy** format")

        elif not starting_time:
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"state": "asking_starting_time"}}
            )
            print("Updated state to 'asking_starting_time'")
            return jsonify( "Please provide the starting time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

        elif not ending_time:
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"state": "asking_ending_time"}}
            )
            print("Updated state to 'asking_ending_time'")
            return jsonify( "Please provide the ending time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

        else:
            # Proceed with booking or conflict check
            return check_for_conflicts_and_book(phone_number, hall_name, meeting_date, starting_time, ending_time)

    if state == 'asking_cab_booking_id':
        # Convert input text to uppercase to handle case sensitivity
        text = text.upper()

        # Split input by spaces, commas, periods, and hyphens to handle multiple IDs
        cab_booking_ids = re.split(r"[ ,.-]+", text)  # Splits on space, comma, period, and hyphen

        # Regex pattern to check format C followed by 6 digits
        valid_id_format = re.compile(r"^C\d{6}$")

        # Check if more than two cab booking IDs were provided
        if len(cab_booking_ids) > 2:
            return jsonify("You have provided more than two cab booking IDs. Please provide a maximum of two valid booking IDs.")

        # Initialize a list to track invalid IDs (either incorrect format or too short)
        invalid_ids = []

        # Check each ID for validity
        for cab_booking_id in cab_booking_ids:
            # Check if the ID length is less than 7 (invalid)
            if len(cab_booking_id) < 7:
                invalid_ids.append(cab_booking_id)
            # Check if the ID does not match the valid format
            elif not valid_id_format.match(cab_booking_id):
                invalid_ids.append(cab_booking_id)

        # If there are any invalid IDs, show an error
        if invalid_ids:
            return jsonify(f"Invalid cab booking ID(s): {', '.join(invalid_ids)}. Cab booking IDs must start with 'C' and be followed by 6 digits (e.g., C123456). Please provide valid ID(s).")

        # If validation passes, clear any error and update the state with the valid cab booking ID
        if cab_booking_ids:
            cab_booking_id = cab_booking_ids[0]  # Take the first valid ID

            # Update the conversation state to remove previous error and save the valid ID
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {
                    "cab_booking_id": cab_booking_id,
                    "error_cab_booking_id": "",  # Clear any previous error
                    "state": ""  # Clear the state if no further input is needed
                }}
            )

            print(f"Valid cab booking ID received: {cab_booking_id}")

            # Proceed with the cab cancellation
            return cab_cancelling_id(phone_number, cab_booking_id)

    if state == 'asking_meeting_booking_id':
        # Convert input text to uppercase to handle case sensitivity
        text = text.upper()

        # Split input by spaces, commas, periods, and hyphens to handle multiple IDs
        meeting_booking_ids = re.split(r"[ ,.-]+", text)  # Splits on space, comma, period, and hyphen

        # Regex pattern to check format M followed by 6 digits
        valid_id_format = re.compile(r"^M\d{6}$")

        # Check if more than two meeting booking IDs were provided
        if len(meeting_booking_ids) > 2:
            return jsonify("You have provided more than two meeting booking IDs. Please provide a maximum of two valid booking IDs.")

        # Initialize a list to track invalid IDs (either incorrect format or too short)
        invalid_ids = []

        # Check each ID for validity
        for meeting_booking_id in meeting_booking_ids:
            # Check if the ID length is less than 7 (invalid)
            if len(meeting_booking_id) < 7:
                invalid_ids.append(meeting_booking_id)
            # Check if the ID does not match the valid format
            elif not valid_id_format.match(meeting_booking_id):
                invalid_ids.append(meeting_booking_id)

        # If there are any invalid IDs, show an error
        if invalid_ids:
            return jsonify(f"Invalid Meeting booking ID(s): {', '.join(invalid_ids)}. Meeting booking IDs must start with 'M' and be followed by 6 digits (e.g., M123456). Please provide valid ID(s).")

        # If validation passes, clear any error and update the state with the valid meeting booking ID
        if meeting_booking_ids:
            meeting_booking_id = meeting_booking_ids[0]  # Take the first valid ID

            # Update the conversation state to remove previous error and save the valid ID
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {
                    "meeting_booking_id": meeting_booking_id,
                    "error_meeting_booking_id": "",  # Clear any previous error
                    "state": ""  # Clear the state if no further input is needed
                }}
            )

            print(f"Valid meeting booking ID received: {meeting_booking_id}")

            # Proceed with the meeting cancellation
            return meeting_cancelling_id(phone_number, meeting_booking_id)





    if state == 'asking_dropoff_point':
        user_input = text.strip().lower()  # Normalize user input for case-insensitive matching
    
        # List of drop-off points (converted to lowercase for comparison)
        dropoff_points = ["elcot main gate", "madurai kamaraj college", "nagamalai puthukottai", "achampathu", "kalavasal"]
    
        # Normalize spaces by reducing any multiple spaces to a single space
        user_input = re.sub(r'\s+', ' ', user_input)
    
        # Split user input by spaces to detect multiple drop-off points
        dropoff_points_provided = user_input.split(',')
        
        # Check if multiple valid drop-off points are provided
        valid_dropoff_points = [point.strip() for point in dropoff_points_provided if point.strip() in dropoff_points]
        
        if len(valid_dropoff_points) > 1:
            # If multiple valid drop-off points are provided, ask the user to choose one
            return jsonify("Multiple drop-off points detected. Please provide only one drop-off point from the list.")
        
        elif len(valid_dropoff_points) == 0:
            # If no valid drop-off point is provided, return an invalid drop-off point message
            return jsonify("Invalid drop-off point. Please enter a valid drop-off point from the list.")
        
        # If only one valid drop-off point is provided, proceed
        selected_dropoff_point = valid_dropoff_points[0]
        print(f"Valid drop-off point selected: {selected_dropoff_point}")
    
        # Fetch the starting time and booking date from the conversation state
        starting_time_str = conversation_state.get('starting_time')
        starting_time = datetime.strptime(starting_time_str, "%H:%M").time()
    
        # Fetch the booking date from the conversation state (assuming the format is dd/mm/yyyy)
        booking_date_str = conversation_state.get('meeting_date')  # Assume meeting_date is stored as string in format 'dd/mm/yyyy'
        booking_date = datetime.strptime(booking_date_str, "%d/%m/%Y").date()
    
        # Get the current date and time in the Asia/Kolkata timezone
        tz = pytz.timezone('Asia/Kolkata')
        current_time = datetime.now(tz).time()
        current_date = datetime.now(tz).date()
    
        # Check if the starting time is valid (18:30 or 19:30)
        if not is_valid_time_for_cabs(starting_time):
            return jsonify("Cab time is invalid. Please enter 18:30 or 19:30 as the starting time.")
    
        # Cab details with stop names
        cab1_stops = ["elcot main gate", "madurai kamaraj college"]  # Cab 1 stops
        cab2_stops = ["elcot main gate", "madurai kamaraj college", "nagamalai puthukottai", "achampathu", "kalavasal"]  # Cab 2 stops
    
        # Check if the booking is for today and compare current time with the starting time
        if booking_date == current_date:  # Only check if booking is for today
            if starting_time == datetime.strptime("18:30", "%H:%M").time():
                if current_time > starting_time:
                    return ask_user_to_wait_or_exit(phone_number, "The cab will not arrive as it has already left. Please choose options: 1) **Cab 2** 2) **Exit**.") 
    
                    # Update the state for asking late 6:30 batch
                    conversation_state_collection.update_one(
                        {"phone_number": phone_number},
                        {"$set": {
                            "state": "asking_late_6:30_batch",
                            "options": ["Cab 2", "Exit"]
                        }},
                        upsert=True
                    )
    
            elif starting_time == datetime.strptime("19:30", "%H:%M").time():
                if current_time > starting_time:
                    conversation_state_collection.delete_one({"phone_number": phone_number})  # Remove conversation state
                    return jsonify("Both cabs have already left. Please contact the administrative office. The conversation state has been removed.")
    
        # Define available cabs based on the starting time and user drop-off point
        available_cabs = []
    
        if starting_time <= datetime.strptime("18:30", "%H:%M").time():
            if selected_dropoff_point in cab1_stops:
                available_cabs.append("Cab 1")
            if selected_dropoff_point in cab2_stops:
                available_cabs.append("Cab 2")
        elif datetime.strptime("18:30", "%H:%M").time() < starting_time <= datetime.strptime("19:30", "%H:%M").time():
            if selected_dropoff_point in cab2_stops:
                available_cabs.append("Cab 2")
    
        # If there are available cabs, show them along with the "Exit" option
        if available_cabs:
            available_cabs.append("Exit")  # Always show the Exit option
            option_message = "Available cabs are: " + " ".join([f"*{i+1}) {cab}*" for i, cab in enumerate(available_cabs)])
    
            # Update the state to 'asking_cab_selection' and store the available cabs
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {
                    "state": "asking_cab_selection",
                    "options": available_cabs,  # Store available cabs for selection
                    "dropping_point": selected_dropoff_point  # Store selected drop-off point
                }},
                upsert=True
            )
    
            return jsonify(option_message + " Please select a cab by entering the option number")
        else:
            return jsonify("No cabs are available for your selected drop-off point.")


    
    if state == 'asking_late_6:30_batch':
        option = text.strip()  # Normalize input by stripping spaces
    
        # Normalize input: remove spaces, split by spaces or commas, and convert to lowercase
        options_provided = re.split(r'\s+|,', option.lower().strip())
    
        # Valid options for the user to choose
        valid_options = {
            "cab2": "Cab 2",
            "1": "Cab 2",  # Allow '1' as a shortcut for 'Cab 2'
            "exit": "Exit",
            "2": "Exit"    # Allow '2' as a shortcut for 'Exit'
        }
    
        # Filter out valid options from user input
        selected_options = [opt for opt in options_provided if opt in valid_options]
    
        # Check if multiple valid options were provided
        if len(selected_options) > 1:
            return jsonify("Multiple options detected. Please choose only one option: 1) Cab 2 or 2) Exit.")
    
        # If no valid option is provided
        if len(selected_options) == 0:
            return jsonify("Invalid option. Please enter either 'Cab 2' or 'Exit'.")
    
        # If exactly one valid option is provided
        selected_option = selected_options[0]
    
        if selected_option == "cab2" or selected_option == "1":
            # Cab 2 booking process
            cab_name = "Cab 2"
            existing_ids = [doc.get('booking_id') for doc in cab_booking_collection.find({}, {'_id': 0, 'booking_id': 1}) if doc.get('booking_id') is not None]
            booking_id = generate_unique_id(existing_ids)
    
            # Insert booking into MongoDB
            cab_booking_collection.insert_one({
                "booking_id": booking_id,
                "phone_number": phone_number,
                "cab_name": cab_name,
                "starting_time": conversation_state.get('starting_time'),
                "meeting_date": conversation_state.get('meeting_date'),
                "dropping_point": conversation_state.get('dropping_point'),
            })
    
            # Remove conversation state
            conversation_state_collection.delete_one({"phone_number": phone_number})
            return jsonify(f"{cab_name} has been booked successfully. Your booking ID is {booking_id}.")
    
        elif selected_option == "exit" or selected_option == "2":
            # Remove conversation state and exit the conversation
            conversation_state_collection.delete_one({"phone_number": phone_number})
            return jsonify("Thank you! The conversation has been ended.")



    


    elif state == 'asking_cab_selection':
        # Normalize the user input for case-insensitive comparison and trim spaces
        user_input = text.strip().lower().replace(" ", "")  # Remove spaces and convert to lowercase
        
        # Split the input by commas, spaces, or other delimiters to detect multiple values
        user_input_values = re.split(r'[,;\s]+', user_input)  # Splits by comma, semicolon, or space
        
        # Remove empty strings in case of extra spaces or delimiters
        user_input_values = list(filter(None, user_input_values))
        
        # Fetch the available options (e.g., ['Cab 1', 'Cab 2', 'Exit'])
        options = conversation_state.get('options', [])  # Ensure options is fetched and initialized as a list
        
        # Create a mapping for easier comparison and mapping numeric inputs to options
        option_mapping = {
            "1": "Cab 1",
            "2": "Cab 2",
            "3": "Exit"
        }
        
        # Add actual option names to the mapping (e.g., "cab1" -> "Cab 1", "exit" -> "Exit")
        for option in options:
            normalized_option = option.lower().replace(" ", "")  # Normalize the option
            option_mapping[normalized_option] = option  # Map normalized to actual option
        
        # Check if multiple values have been provided by the user
        if len(user_input_values) > 1:
            return jsonify("Multiple options detected. Please select only one valid option: 1) Cab 1, 2) Cab 2, or 3) Exit.")
        
        # Check if the single user input is a valid option by looking it up in the mapping
        user_input = user_input_values[0]  # Extract the first and only input after ensuring there's only one
        if user_input not in option_mapping:
            return jsonify("Invalid option. Please choose a valid option: 1) Cab 1, 2) Cab 2, or 3) Exit.")
        
        # Find the selected option (maintain original casing from the options list)
        selected_option = option_mapping[user_input]  # Fetch original option based on normalized input
        
        # Handle the "Exit" option
        if selected_option.lower() == "exit":
            # Clear the conversation state for the user (end the session)
            conversation_state_collection.delete_one({"phone_number": phone_number})
            return jsonify("Thank you! The conversation has been ended.")
        
        else:
            # Determine the cab name based on the selected option
            cab_name = selected_option  # The user-selected cab (e.g., 'Cab 1' or 'Cab 2')
        
            # Fetch existing booking IDs to ensure uniqueness
            existing_ids = [doc.get('booking_id') for doc in cab_booking_collection.find({}, {'_id': 0, 'booking_id': 1}) if doc.get('booking_id') is not None]
            booking_id = generate_unique_id(existing_ids)
        
            # Insert booking into MongoDB
            cab_booking_collection.insert_one({
                "booking_id": booking_id,
                "phone_number": phone_number,
                "cab_name": cab_name,
                "starting_time": conversation_state.get('starting_time'),
                "meeting_date": conversation_state.get('meeting_date'),
                "dropping_point": conversation_state.get('dropping_point'),
            })
        
            # Clear the conversation state after booking
            conversation_state_collection.delete_one({"phone_number": phone_number})
            return jsonify(f"{cab_name} has been booked successfully. Your booking ID is {booking_id}.")
    




    if state == 'asking_meeting_date':
    # Extract and convert dates from text
      intent=conversation_state.get('intent')
      print(f"Received intent: {intent}")
      print(f"Received meeting date: {text}")
      dates = extract_dates(text)
      converted_dates = convert_dates(dates)
      intent = conversation_state.get('intent')
      print(f"Extracted dates: {converted_dates}")

      if not converted_dates:
          return jsonify("Please provide a valid date in the format 'dd/mm/yyyy' or similar.")

      meeting_date = converted_dates[0]
      validation_error = validate_meeting_date(meeting_date)
      current_date = datetime.now().date()
      print("Going into the meeting date validation logic.")
      meeting_date_obj = datetime.strptime(meeting_date, '%d/%m/%Y').date()
      meeting_year = meeting_date_obj.year
      future_year_limit = 2024
      if meeting_year > future_year_limit:
            return jsonify(f"Please do not provide a date in the future beyond {future_year_limit}.")

      if meeting_date_obj < current_date:
          return jsonify( "Please provide a date that is not in the past.")

      if validation_error:
          return jsonify({ validation_error})

      if intent == 'meeting_booking':
          hall_name = conversation_state.get('hall_name')
          starting_time = conversation_state.get('starting_time')
          ending_time = conversation_state.get('ending_time')

          print(f"[DEBUG] Hall Name: {hall_name}, Starting Time: {starting_time}, Ending Time: {ending_time}")

          if not hall_name:
              conversation_state_collection.update_one(
                  {"phone_number": phone_number},
                  {"$set": {"meeting_date": meeting_date, "state": "asking_hall_name"}}
              )
              print("Updated state to 'asking_hall_name'")
              return jsonify( "Please provide the hall name.")

          elif not starting_time:
              conversation_state_collection.update_one(
                  {"phone_number": phone_number},
                  {"$set": {"meeting_date": meeting_date, "state": "asking_starting_time"}}
              )
              print("Updated state to 'asking_starting_time'")
              return jsonify("Please provide the starting time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

          elif not ending_time:
              conversation_state_collection.update_one(
                  {"phone_number": phone_number},
                  {"$set": {"meeting_date": meeting_date, "state": "asking_ending_time"}}
              )
              print("Updated state to 'asking_ending_time'")
              return jsonify( "Please provide the ending time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

          else:
              return check_for_conflicts_and_book(phone_number, hall_name, meeting_date, starting_time, ending_time)

      elif intent == 'cab_booking':
          starting_time=conversation_state.get('starting_time')
          print(f"No starting_time provided")

          # Check if cab_name is missing
          if not starting_time:
              conversation_state_collection.update_one(
                  {"phone_number": phone_number},
                  {"$set": {"meeting_date": meeting_date, "state": "asking_starting_time"}}
              )
              print("Updated state to 'asking_starting_time'")
              return jsonify("Please provide the starting time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")
          else:
              print(f"[DEBUG] Proceeding to cab booking")
              return handle_cab_selection(phone_number,starting_time,meeting_date)

      elif intent =="list_cabs_booked":
          print(f"Received list_cabs_booked intent")
          return handle_cab_booking_stats(phone_number,meeting_date)
      elif intent =="list_meetings_booked":
          print(f"Received meeting_booked intent")
          return handle_meeting_booking_stats(phone_number,meeting_date)









    if state == 'asking_starting_time':
        text = text.strip()
        intent = conversation_state.get('intent')
        print(f"Received starting time: {text}")

        # Extract starting time from text
        starting_time, time_error = extract_times(text)
        if time_error:
            return jsonify({"message": time_error})

        if not starting_time:
            return jsonify("Please provide a valid starting time.")

        # Convert the starting time to 24-hour format
        starting_time_24h = convert_to_24_hour_format(starting_time)
        if not starting_time_24h:
            return jsonify("Invalid time format. Please provide a valid time in 'HH:MM AM/PM' format.")

        print(f"Received starting time in 24-hour format: {starting_time_24h}")

        # Retrieve the meeting date from the conversation state
        meeting_date_str = conversation_state.get('meeting_date')
        if not meeting_date_str:
            return jsonify({"message": "Meeting date is missing. Please provide the meeting date."})

        # Convert the meeting date string to a date object
        meeting_date = datetime.strptime(meeting_date_str, '%d/%m/%Y').date()

        # Check if the meeting date is today
        current_date = datetime.now().date()

        if meeting_date == current_date:
            # If the meeting is today, get the current time
            current_time = datetime.now().time()
            starting_time_obj = datetime.strptime(starting_time_24h, "%H:%M").time()

            # Debugging logs
            print(f"Current time: {current_time}")
            print(f"Starting time provided: {starting_time_obj}")

            # Check if the provided starting time is greater than the current time
            if starting_time_obj <= current_time:
                return jsonify("Starting time is less than the current time. Please provide a future time for today.")

        # Intent-specific handling
        if intent == 'meeting_booking':
            # Check if hall_name is present, if not, ask again
            hall_name = conversation_state.get('hall_name')
            if not hall_name:
                return jsonify("Hall name is missing. Please provide the hall name.")

            # Store starting time and update state to ask for ending time
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {
                    "starting_time": starting_time_24h,  # Save validated starting time
                    "state": "asking_ending_time"  # Update the state to ask for the ending time
                }}
            )
            print("Updated state to 'asking_ending_time'")
            return jsonify("Please provide the ending time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

        elif intent == 'cab_booking':
            # Save the validated starting time in conversation state
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"starting_time": starting_time_24h}}
            )

            # Debugging before calling complete_cab_booking
            print(f"[DEBUG] Starting cab booking process.")
            print(f"Phone number: {phone_number}")
            print(f"Meeting date: {meeting_date}")
            print(f"Starting time (24-hour format): {starting_time_24h}")

            meeting_date=meeting_date_str
            print(f"Meeting date correted form: {meeting_date}")

            # Proceed to complete the cab booking
            starting_time = starting_time_24h  # Pass the 24-hour format time
            valid_times = ["18:30", "19:30"]

            # Check if the entered time is valid
            if starting_time not in valid_times:
                # Raise error message if the time is not 18:30 or 19:30
                error_message = "Cabs are not available at the selected time. Please enter a valid time: 6:30pm or 7:30."
                print(error_message)

                # Remove the 'starting_time' from the conversation state
                conversation_state_collection.update_one(
                    {"phone_number": phone_number},
                    {"$unset": {"starting_time": ""}, "$set": {"state": "asking_starting_time"}},
                    upsert=True
                )

                return jsonify(error_message)



            # Proceed to handle cab selection after valid time check
            print(f"Proceeding to complete cab booking with starting time: {starting_time}")
            return handle_cab_selection(phone_number, starting_time, meeting_date)







    if state == 'asking_cab_validation':
      user_input = text.strip().lower()  # Normalize user input for case-insensitive matching

      # Check the user input for both 1 and variations of "Re-enter the details starting from the meeting date"
      if user_input == '1' or 're-enter the details starting from the meeting date'  or 'reenter' or 'Re-enter'in user_input.lower():
        # Remove everything from the conversation state except intent and phone number
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "asking_meeting_date",  # Set state to ask for meeting date
                "intent": "cab_booking"  # Retain the intent
            }, "$unset": {
                "meeting_date": "",
                "starting_time": "",
            }},
            upsert=True
        )

        # Ask for the meeting date again
        return jsonify("Please provide the meeting date in **dd/mm/yyyy** format.")

    # If user chose to exit
      elif user_input == '2' or 'exit' or 'Exit' in user_input.lower():
        # Remove everything from the conversation state
        conversation_state_collection.delete_one({"phone_number": phone_number})
        # Send a farewell message
        return jsonify("Thank you for using our cab booking service. Your session has been closed.")

    elif state == 'asking_meeting_first_options':
      user_input = text.strip().lower()  # Normalize user input for case-insensitive matching

      # Check if the user chose to start over or exit
      if user_input in ["1", "start over", "start over by entering the hall name again"]:
          # Remove everything except phone number and intent from conversation state
          conversation_state_collection.update_one(
              {"phone_number": phone_number},
              {"$unset": {  # Remove everything except the phone number and intent
                  "hall_name": "",
                  "meeting_date": "",
                  "starting_time": "",
                  "ending_time": "",
                  "other_fields": ""  # Add any other fields that need to be cleared
              },
              "$set": {
                  "state": "asking_hall_name"  # Set state to ask for hall name again
              }}
          )

          # Inform the user that they can start over by entering the hall name
          return jsonify("Starting over. Please provide the hall name"
          "Available halls are: New York, Mumbai, Huston, Amsterdam, Delhi, Tokyo, Chicago, 0a, 0b, 0c, 1a, 1b, 1c, 2a, 2b, 2c.")

      elif user_input in ["2", "exit"]:
          # Remove the entire conversation state as the user opted to exit
          conversation_state_collection.delete_one({"phone_number": phone_number})

          # Inform the user that the old conversation state has been removed
          return jsonify("You have exited the process. The previous conversation state has been removed.")

      else:
          # Handle invalid input (user didn't enter 1, 2, or valid option)
          return jsonify("Invalid option. Please enter '1' to start over or '2' to exit.")

    elif state == 'asking_ending_time':
        print(f"Received ending time: {text}")
        ending_time, time_error = extract_times(text)
        if time_error:
            return jsonify( time_error)

        if not ending_time:
            return jsonify( "Please provide a valid ending time.")

        ending_time_24h = convert_to_24_hour_format(ending_time)

        if not ending_time_24h:
            return jsonify("Invalid time format. Please provide a valid time in 'HH:MM AM/PM' format.")

        print(f"Received ending time in 24-hour format: {ending_time_24h}")

        # Retrieve starting time and hall name
        starting_time_24h = conversation_state.get('starting_time')
        hall_name = conversation_state.get('hall_name')

        if not hall_name:
            return jsonify( "Hall name is missing. Please provide the hall name.")

        # Validate that starting time is less than ending time
        if starting_time_24h >= ending_time_24h:
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$unset": {"starting_time": "", "ending_time": ""}, "$set": {"state": "asking_starting_time"}}
            )
            return jsonify("Starting time must be less than ending time. Please provide the starting time again.")

        # Update conversation state with ending time
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"ending_time": ending_time_24h}}
        )

        print(f"Starting time {starting_time_24h} and Ending time {ending_time_24h} are valid.")

        # Proceed with conflict check or booking
        return check_for_conflicts_and_book(phone_number, hall_name, conversation_state['meeting_date'], starting_time_24h, ending_time_24h)

    elif state == 'choosing_conflict_option':
        # Process conflict resolution options here
        print(f"Processing conflict resolution option for phone number: {phone_number}")
        user_input = text.strip().lower()
        if user_input == '1' or 'different hall' in user_input.lower():
            # Recommending available halls
            return recommend_available_halls(phone_number, conversation_state)
        elif  user_input == '2' or 'different date' in user_input.lower():
            # Option 2: Choose a new date/time, reset date and time
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$unset": {"meeting_date": "", "starting_time": "", "ending_time": ""},
                 "$set": {"state": "asking_new_meeting_date"}}
            )
            return jsonify("Please provide a new meeting date in **dd/mm/yyyy format.")
        else:
            return jsonify( "Invalid option. Please select 1 to see available halls or 2 to choose a new date.")

    elif state == 'asking_new_meeting_date':
        # Extract and validate new meeting date
        dates = extract_dates(text)
        converted_dates = convert_dates(dates)

        if not converted_dates:
            return jsonify("Please provide a valid date in the format 'dd/mm/yyyy'")

        new_meeting_date = converted_dates[0]
        validation_error = validate_meeting_date(new_meeting_date)
        new_meeting_date_obj = datetime.strptime(new_meeting_date, '%d/%m/%Y').date()
        current_date = datetime.now().date()
        if current_date > new_meeting_date_obj :
            return jsonify( "Please provide a date that is not in the past.")


        if validation_error:
            return jsonify( validation_error)

        # Update the state to ask for new starting time
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "meeting_date": new_meeting_date,
                "state": "asking_new_starting_time"
            }}
        )

        # Fetch available time slots for the selected hall and new date
        available_time_slots = get_available_time_slots(conversation_state.get('hall_name'), new_meeting_date)
        return jsonify( f"The hall is available at the following times: {available_time_slots}. Please provide the new starting time in **h:mm am/pm(3:00pm/ 4:15pm)** format.")

    elif state == 'asking_new_starting_time':
    # Validate new starting time
      new_starting_time, time_error = extract_times(text)
      if time_error:
          return jsonify( time_error)

      new_starting_time_24h = convert_to_24_hour_format(new_starting_time)
      if not new_starting_time_24h:
          return jsonify("Invalid time format. Please provide the starting time in 'HH:MM AM/PM' format.")

      # Retrieve the meeting date from the conversation state
      meeting_date_str = conversation_state.get('meeting_date')
      if not meeting_date_str:
          return jsonify( "Meeting date is missing. Please provide the meeting date in dd/mm/yyyy format.")

      # Convert the meeting date string to a date object
      meeting_date = datetime.strptime(meeting_date_str, '%d/%m/%Y').date()

      # Check if the meeting date is today
      current_date = datetime.now(local_tz).date()

      if meeting_date == current_date:
          # If the meeting is today, get the current time in the correct timezone
          current_time = datetime.now(local_tz).time()
          new_starting_time_obj = datetime.strptime(new_starting_time_24h, "%H:%M").time()

          # Debugging logs
          print(f"Current time: {current_time}")
          print(f"New starting time provided: {new_starting_time_obj}")

          # Check if the provided new starting time is greater than the current time
          if new_starting_time_obj <= current_time:
              return jsonify( "New starting time is less than the current time. Please provide a future time for today.")

      # Store starting time and update state to ask for ending time
      conversation_state_collection.update_one(
          {"phone_number": phone_number},
          {"$set": {
              "starting_time": new_starting_time_24h,
              "state": "asking_new_ending_time"
          }}
      )
      return jsonify( "Please provide the new ending time.")



    elif state == 'asking_new_ending_time':
        # Validate new ending time
        new_ending_time, time_error = extract_times(text)
        if time_error:
            return jsonify( time_error)

        new_ending_time_24h = convert_to_24_hour_format(new_ending_time)
        if not new_ending_time_24h:
            return jsonify( "Invalid time format. Please provide the ending time in 'HH:MM AM/PM' format.")

        # Retrieve starting time, hall name, and meeting date from the conversation state
        new_starting_time_24h = conversation_state.get('starting_time')
        hall_name = conversation_state.get('hall_name')
        new_meeting_date = conversation_state.get('meeting_date')

        # Validate that starting time is less than ending time
        if new_starting_time_24h >= new_ending_time_24h:
            # If invalid, clear starting and ending time and prompt for new starting time
            conversation_state_collection.update_one(
                {"phone_number": phone_number},
                {"$unset": {"starting_time": "", "ending_time": ""},
                 "$set": {"state": "asking_new_starting_time"}}
            )
            return jsonify( "Starting time must be earlier than the ending time. Please provide a new starting time.")

        existing_ids = []

        for doc in meeting_booking_collection.find({}, {'_id': 0, 'booking_ids': 1}):
            if 'booking_ids' in doc:
                existing_ids.append(doc['booking_ids'])
            else:
                existing_ids.append(0)  # or handle it as needed

        # If you need to ensure existing_ids contains only unique values
        existing_ids = list(set(existing_ids))
            # Generate a unique booking ID
        bookings_id = generate_unique_ids(existing_ids)



        # Insert the new booking into the database
        booking_id = meeting_booking_collection.insert_one({
            "phone_number": phone_number,
            "hall_name": hall_name,
            "bookings_id":bookings_id,
            "meeting_date": new_meeting_date,
            "starting_time": new_starting_time_24h,
            "ending_time": new_ending_time_24h
        }).inserted_id

        # Clear the conversation state after booking
        conversation_state_collection.delete_one({"phone_number": phone_number})

        return jsonify(f"Meeting successfully booked at {hall_name} on {new_meeting_date} from {new_starting_time_24h} to {new_ending_time_24h}  with meeting id {bookings_id}")


    elif state == 'recommending_hall':
        # Handle the state where the user is choosing a new hall from recommendations
        hall_name = text.strip().title()
        # Inside the recommending_hall state handler
        if hall_name not in hall_names_with_webex + small_halls:
          print(f"Invalid hall name received: {hall_name}")
          print(f"Valid hall names: {hall_names_with_webex + small_halls}")
          return jsonify({"message": "Invalid hall name. Please choose from the available options."})


        # Proceed with booking the chosen hall
        meeting_date = conversation_state.get('meeting_date')
        starting_time = conversation_state.get('starting_time')
        ending_time = conversation_state.get('ending_time')
        return check_for_conflicts_and_book(phone_number, hall_name, meeting_date, starting_time, ending_time)

    return jsonify("Unhandled conversation state.")



from datetime import datetime
import pymongo

def check_for_conflicts_and_book(phone_number, hall_name, meeting_date, starting_time, ending_time):
    print(f"Checking for conflicts for hall: {hall_name}, date: {meeting_date}, start: {starting_time}, end: {ending_time}")

    existing_ids = []

    # Fetch all existing booking IDs
    for doc in meeting_booking_collection.find({}, {'_id': 0, 'booking_id': 1}):
        if 'booking_id' in doc:
            existing_ids.append(doc['booking_id'])
        else:
            existing_ids.append(0)  # Handle if no booking ID exists

    # Ensure unique booking IDs by removing duplicates
    existing_ids = list(set(existing_ids))

    # Generate a unique booking ID for the new booking
    booking_id = generate_unique_ids(existing_ids)

    # Step 1: Check if the user has a conflicting meeting on the same date where the times overlap
    user_conflicting_booking = meeting_booking_collection.find_one({
        "phone_number": phone_number,
        "meeting_date": meeting_date,
        "$or": [
            {"starting_time": {"$lt": ending_time, "$gte": starting_time}},  # New meeting starts during an existing meeting
            {"ending_time": {"$gt": starting_time, "$lte": ending_time}},    # New meeting ends during an existing meeting
            {"starting_time": {"$lte": starting_time}, "ending_time": {"$gte": ending_time}}  # New meeting is fully within an existing one
        ]
    })

    if user_conflicting_booking:
        # Step 2: Inform the user about the conflict and provide options
        existing_start_time = user_conflicting_booking['starting_time']
        existing_end_time = user_conflicting_booking['ending_time']
        existing_hall_name = user_conflicting_booking['hall_name']  # Fetch the hall name for the conflicting booking

        # Calculate available time slots (from 12 AM to 12 PM)
        available_slots = get_available_time_slotss(phone_number, meeting_date)

        # Format available time slots for the response
        available_time_msg = "\n".join(available_slots)

        # Update state to ask the user for next steps: start over or exit
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "asking_meeting_first_options",  # State asking for the user's next action
            }}
        )

        # Return a response asking for user's choice with two options and show the conflict details and available times
        return jsonify(
            f"You already have a meeting booked at **{existing_hall_name}** on {meeting_date} from {existing_start_time} to {existing_end_time}. "
            f"Here are your available time slots for the day (from 12 AM to 12 PM):\n"
            f"{available_time_msg}\n"
            f"So please enter the option number or value from the following:\n"
            f"**1)** Start over by entering the hall name again\n"
            f"**2)** Exit"
        )

    # Step 3: Check if the selected hall has any conflicting bookings at the requested time
    hall_conflicting_booking = meeting_booking_collection.find_one({
        "hall_name": hall_name,
        "meeting_date": meeting_date,
        "$or": [
            {"starting_time": {"$lt": ending_time, "$gte": starting_time}},  # Hall booked at this time
            {"ending_time": {"$gt": starting_time, "$lte": ending_time}},    # Hall booked at this time
            {"starting_time": {"$lte": starting_time}, "ending_time": {"$gte": ending_time}}  # Hall fully booked during this time
        ]
    })

    if hall_conflicting_booking:
        print("Conflict found with existing booking for the selected hall.")

        # Update state to let user choose between changing hall or date, and store the meeting_date
        conversation_state_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "state": "choosing_conflict_option",
                "meeting_date": meeting_date
            }}
        )
        print("Updated state to 'choosing_conflict_option'")

        return jsonify("The selected hall is not available at the requested time. Please enter **1** to choose a **different hall** or **2** to choose a **different date**.")

    # Step 4: No conflicts, proceed with booking
    booking_ids = meeting_booking_collection.insert_one({
        "phone_number": phone_number,
        "hall_name": hall_name,
        "bookings_id": booking_id,
        "meeting_date": meeting_date,
        "starting_time": starting_time,
        "ending_time": ending_time
    }).inserted_id

    conversation_state_collection.delete_one({"phone_number": phone_number})
    print("Booking successful and conversation state removed.")

    return jsonify(f"Meeting successfully booked at {hall_name} on {meeting_date} from {starting_time} to {ending_time} with booking ID {booking_id}")

def get_available_time_slotss(phone_number, meeting_date):
    # Define opening and closing times
    opening_time = datetime.strptime('00:00', '%H:%M')
    closing_time = datetime.strptime('23:59', '%H:%M')

    # Find existing bookings for the specified phone number and date
    bookings = meeting_booking_collection.find({
        "phone_number": phone_number,  # Ensure the key matches your MongoDB schema
        "meeting_date": meeting_date     # Ensure the key matches your MongoDB schema
    }).sort("starting_time", pymongo.ASCENDING)

    available_slots = []
    current_time = opening_time

    # Iterate through existing bookings to determine available slots
    for booking in bookings:
        booking_start = datetime.strptime(booking['starting_time'], '%H:%M')
        booking_end = datetime.strptime(booking['ending_time'], '%H:%M')

        # Check if there is free time before the next booking
        if current_time < booking_start:
            # Add available slot before the next booking
            available_slots.append(f"{current_time.strftime('%H:%M')} - {booking_start.strftime('%H:%M')}")

        # Update current time to the end of the current booking
        current_time = max(current_time, booking_end)

    # Add a slot for the remaining time if available
    if current_time < closing_time:
        available_slots.append(f"{current_time.strftime('%H:%M')} - {closing_time.strftime('%H:%M')}")

    # Return available slots or indicate no slots are available
    return available_slots if available_slots else ["No available slots"]







    

   
   


def get_available_time_slots(hall_name, meeting_date):
    # Define the hall's opening and closing times
    opening_time = datetime.strptime('09:00', '%H:%M')
    closing_time = datetime.strptime('22:00', '%H:%M')

    # Find existing bookings for the specified hall and date
    bookings = meeting_booking_collection.find({
        "hall_name": hall_name,
        "meeting_date": meeting_date
    }).sort("starting_time", pymongo.ASCENDING)

    available_slots = []
    current_time = opening_time

    # Iterate through existing bookings to determine available slots
    for booking in bookings:
        booking_start = datetime.strptime(booking['starting_time'], '%H:%M')
        booking_end = datetime.strptime(booking['ending_time'], '%H:%M')

        if current_time < booking_start:
            # Add available slot before the next booking
            available_slots.append(f"{current_time.strftime('%H:%M')} - {booking_start.strftime('%H:%M')}")

        # Update current time to the end of the current booking
        current_time = max(current_time, booking_end)

    # Add a slot for the remaining time if available
    if current_time < closing_time:
        available_slots.append(f"{current_time.strftime('%H:%M')} - {closing_time.strftime('%H:%M')}")

    return available_slots if available_slots else "No available slots"


def recommend_available_halls(phone_number, conversation_state):
    meeting_date = conversation_state.get("meeting_date")
    starting_time = conversation_state.get("starting_time")
    ending_time = conversation_state.get("ending_time")

    if not meeting_date or not starting_time or not ending_time:
        return jsonify("Missing required data to recommend available halls.")

    # Find halls that are already booked during the given time
    booked_halls = meeting_booking_collection.distinct("hall_name", {
        "meeting_date": meeting_date,
        "starting_time": {"$lt": ending_time},
        "ending_time": {"$gt": starting_time}
    })

    # Find all halls that are not in the list of booked halls
    all_halls = ['New York', 'Mumbai', 'Houston', 'Amsterdam', 'Delhi', 'Tokyo', 'Chicago', '0a', '0b', '0c', '1a', '1b', '1c', '2a', '2b', '2c']
    available_halls = [hall for hall in all_halls if hall not in booked_halls]

    # Update the state to 'recommending_hall'
    conversation_state_collection.update_one(
        {"phone_number": phone_number},
        {"$set": {"state": "recommending_hall"}}
    )
    print(f"Updated state to 'recommending_hall', available halls: {available_halls}")

    return jsonify( f"The following halls are available on {meeting_date}: {', '.join(available_halls)}. Please select a hall.")



def validate_meeting_date(meeting_date):
    try:
        datetime.strptime(meeting_date, '%d/%m/%Y')
    except ValueError:
        return "Invalid date format. Please provide a date in the format 'dd/mm/yyyy'."
    return None

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

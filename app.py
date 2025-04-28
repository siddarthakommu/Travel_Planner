import os
import json
import sqlite3
import streamlit as st
import google.generativeai as genai
import requests
from dotenv import load_dotenv
from datetime import datetime
import logging
import re

# ---------------- Logging ------------------
logging.basicConfig(
    filename="travel_planner.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# --------------- Load Env Vars ---------------
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
WEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# -------------- Gemini Config ----------------
genai.configure(api_key=GOOGLE_API_KEY)

# --------------- SQLite Setup ----------------
DB_FILE = "trip_plans.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            user_input TEXT,
            destination TEXT,
            json_data TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trip_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL
        )
    """)
    conn.commit()
    conn.close()

def store_trip(user_input, destination, json_data):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO trips (timestamp, user_input, destination, json_data) VALUES (?, ?, ?, ?)", (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_input,
            destination,
            json.dumps(json_data)
        ))
        conn.commit()
        conn.close()
        logging.info(f"Trip stored: {destination}")
    except Exception as e:
        logging.error(f"Failed to store trip: {e}")

def store_token_cost(model, prompt_tokens, completion_tokens):
    # Define cost for input tokens
    if prompt_tokens <= 128000:
        input_cost = prompt_tokens * 0.075 / 1000000  # $0.075 per million tokens
    else:
        input_cost = prompt_tokens * 0.15 / 1000000  # $0.15 per million tokens

    # Define cost for output tokens
    if completion_tokens <= 128000:
        output_cost = completion_tokens * 0.30 / 1000000  # $0.30 per million tokens
    else:
        output_cost = completion_tokens * 0.60 / 1000000  # $0.60 per million tokens

    # Total cost in USD
    total_cost = round(input_cost + output_cost, 6)

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(""" 
            INSERT INTO trip_costs (timestamp, model, prompt_tokens, completion_tokens, total_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            model,
            prompt_tokens,
            completion_tokens,
            prompt_tokens + completion_tokens,
            total_cost
        ))
        conn.commit()
        conn.close()
        logging.info(f"Token cost stored: {prompt_tokens}+{completion_tokens} = {total_cost}")
    except Exception as e:
        logging.error(f"Failed to store token cost: {e}")


init_db()

# ------------- Session State -----------------
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "👋 Hi! I'm your Travel Planner Bot. Tell me about your travel ideas!"}
    ]
if "stored_destinations" not in st.session_state:
    st.session_state.stored_destinations = []
if "last_destination" not in st.session_state:
    st.session_state.last_destination = None

# ------------- Weather API -------------------
def get_weather(city):
    try:
        url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={WEATHER_API_KEY}&units=metric"
        response = requests.get(url)
        data = response.json()
        if data["cod"] != "200":
            print(f"Respnse is not OK: {data}")
            logging.error(f"Response is not OK: {data}")

            return "Could not retrieve weather info."

        forecast = ""
        for i in range(0, 40, 8):
            day = data["list"][i]
            date = day["dt_txt"].split(" ")[0]
            desc = day["weather"][0]["description"].title()
            temp = day["main"]["temp"]
            humidity = day["main"]["humidity"]
            wind = day["wind"]["speed"]
            forecast += f"\n {date}: {desc}, {temp}°C, {humidity}%, {wind} km/h"

        return forecast
    except Exception as e:
        logging.error(f"Weather fetch error: {e}")
        return "Weather info unavailable."

# ----------- Greeting & Travel Detection ----------- 
greeting_keywords = ["hi", "hello", "hey", "good morning", "good evening", "how are you"]
thank_keywords = ["thank you", "thanks", "thankyou", "thx", "appreciate", "grateful"]
non_travel_keywords = ["weather", "news", "joke", "movie", "recipe", "code", "sports"]

def is_greeting(text):
    return any(word in text.lower() for word in greeting_keywords)

def is_thank_you(text):
    return any(word in text.lower() for word in thank_keywords)

def is_non_travel_query(text):
    return any(word in text.lower() for word in non_travel_keywords)

def extract_destination(text):
    patterns = [r"trip to ([a-zA-Z\s]+)", r"visit ([a-zA-Z\s]+)", r"go to ([a-zA-Z\s]+)", r"([a-zA-Z\s]+)$"]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            destination = match.group(1).strip().title()
            if len(destination.split()) <= 3:
                return destination
    return None

# ----------- Trip Generation Logic ----------- 
def generate_trip_response(user_input, destination):
    chat_history = []
    for msg in st.session_state.messages:
        chat_history.append({"role": msg["role"], "parts": [msg["content"]]})
    chat_history.append({"role": "user", "parts": [user_input]})

    system_prompt = """
    You are an expert travel planner assistant.
    When a user asks about a place, respond with:
    1. A friendly markdown travel guide including:
        - Overview
        - Suggested itinerary
        - Attractions
        - Budget tips
        - Hotel and restaurant recommendations
    2. A structured JSON with the following keys:
        - destination
        - overview
        - itinerary
        - attractions
        - budget
        - hotels
        - restaurants
    DO NOT include weather info in the JSON — that will be added separately.
    If the user gives trip details (people, days), ask for missing info politely if needed.
    Always be friendly and follow previous trip context if no new destination is mentioned.
    If the user says thanks and all respond in a friendly manner saying i am always here to help you.
    dont answer questions that are not related to trip and planning, make sure to give a decent replay for not answering.
    """

    try:
        # Generate chat response
        chat = genai.GenerativeModel("gemini-1.5-pro").start_chat(history=chat_history)
        gemini_response = chat.send_message([system_prompt, user_input]).text

        # Parsing the response into markdown and JSON (if present)
        if "json" in gemini_response:
            markdown_part, json_part = gemini_response.split("json")
            json_part = json_part.split("```")[0].strip()
        else:
            markdown_part = gemini_response
            json_part = "{}"

        structured_data = json.loads(json_part)

        # Check if JSON contains real itinerary data
        if "itinerary" in structured_data and destination:
            weather_data = get_weather(destination)
            markdown_part += f"\n\n🌦 *Weather Forecast for {destination} (Next 5 days):*\n{weather_data}"
            structured_data["weather"] = weather_data
            structured_data["destination"] = destination

            # If user asks for hotels, suggest them based on the previous destination
            # if "best hotels" in user_input.lower() and st.session_state.last_destination:
            #     destination = st.session_state.last_destination

            #     # Simulated hotel recommendations (can be adjusted with real API or database)
            #     hotel_recommendations = {
            #         "luxury": ["Hotel Ritz Paris", "Le Meurice", "Shangri-La Hotel"],
            #         "budget": ["Generator Paris", "Ibis Budget Paris", "The 3 Ducks Hostel"],
            #         "family": ["Novotel Paris Centre", "Hotel De La Paix", "Disneyland Paris Hotels"],
            #         "romantic": ["Le Maurice", "Hotel Plaza Athénée", "The Peninsula Paris"],
            #     }

            #     # Assuming user prefers a luxury stay if no preference is specified
            #     recommended_hotels = hotel_recommendations["luxury"]
            #     hotel_list = "\n".join([f"- {hotel}" for hotel in recommended_hotels])

            #     markdown_part += f"\n\n🏨 *Best Hotels in {destination}:*\n{hotel_list}"

            store_trip(user_input, destination, structured_data)
        else:
            structured_data = {}

        prompt_tokens = sum(len(m["parts"][0].split()) for m in chat_history if m["role"] == "user")
        completion_tokens = len(gemini_response.split())
        store_token_cost("gemini-1.5-flash", prompt_tokens, completion_tokens)

        return markdown_part

    except Exception as e:
        logging.error(f"Gemini response error: {e}")
        return "Sorry, something went wrong while planning your trip."

# ---------------- Streamlit UI ---------------- 
st.set_page_config(page_title="Dynamic Travel Planner")
st.title("🧳 Dynamic Travel Planner Chat")
st.markdown("Chat with an AI travel assistant. Get full itinerary, weather, hotels & more.")

# ------------- Display Messages ---------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# -------------- Accept Input ------------------ 
user_input = st.chat_input("Where are you planning to go?")
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    destination = extract_destination(user_input)

    # Use previous destination if none found in this input
    if not destination and st.session_state.last_destination:
        destination = st.session_state.last_destination

    if destination:
        st.session_state.last_destination = destination
        if destination not in st.session_state.stored_destinations:
            st.session_state.stored_destinations.append(destination)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                reply = generate_trip_response(user_input, destination)
                st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})

    elif is_thank_you(user_input):
        bot_msg = "You're very welcome! 😊 I'm always here to help with your travel plans!"
        with st.chat_message("assistant"):
            st.markdown(bot_msg)
        st.session_state.messages.append({"role": "assistant", "content": bot_msg})

    elif is_greeting(user_input):
        bot_msg = "👋 Hello! I'm your friendly Travel Planner. Tell me where you'd like to go!"
        with st.chat_message("assistant"):
            st.markdown(bot_msg)
        st.session_state.messages.append({"role": "assistant", "content": bot_msg})

    elif is_non_travel_query(user_input):
        bot_msg = "I'm focused on helping with travel planning. Ask me about your next trip! 🌍"
        with st.chat_message("assistant"):
            st.markdown(bot_msg)
        st.session_state.messages.append({"role": "assistant", "content": bot_msg})

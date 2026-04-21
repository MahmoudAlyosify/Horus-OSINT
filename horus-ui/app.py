import streamlit as st
import requests
import json

# ==========================================
# 1. PAGE CONFIGURATION
# ==========================================
st.set_page_config(
    page_title="HORUS OSINT TERMINAL",
    page_icon="👁️‍🗨️",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# ==========================================
# 2. CUSTOM CSS (UI/UX IDENTITY)
# ==========================================
# Injects custom CSS for the pulsing live indicator and terminal styling
st.markdown("""
<style>
    /* Pulsing animation for the Live Connection indicator */
    @keyframes pulse {
        0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(0, 255, 65, 0.7); }
        70% { transform: scale(1); box-shadow: 0 0 0 10px rgba(0, 255, 65, 0); }
        100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(0, 255, 65, 0); }
    }
    .pulsing-dot {
        width: 12px;
        height: 12px;
        background-color: #00FF41;
        border-radius: 50%;
        display: inline-block;
        animation: pulse 2s infinite;
    }
    
    /* Header Styling with Horus Gold Border */
    .terminal-header {
        display: flex; 
        align-items: center; 
        justify-content: space-between; 
        border-bottom: 2px solid #D4AF37; 
        padding-bottom: 10px; 
        margin-bottom: 30px;
    }
    
    /* System Status Text */
    .sys-status {
        color: #00FF41; 
        font-family: monospace; 
        font-size: 14px; 
        margin-left: 8px;
        font-weight: bold;
        letter-spacing: 1px;
    }

    /* Force markdown elements (bold, headers, hr) to look tactical */
    hr {
        border-top: 1px dashed #D4AF37;
    }
    h1, h2, h3 {
        color: #D4AF37 !important;
        text-transform: uppercase;
    }
    strong {
        color: #D4AF37;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 3. TERMINAL HEADER
# ==========================================
st.markdown("""
<div class='terminal-header'>
    <div style='display: flex; align-items: center;'>
        <h2 style='margin: 0; font-family: monospace; letter-spacing: 2px;'>
            <span style='font-size: 1.2em;'>🦅</span> HORUS CYBER-INTELLIGENCE TERMINAL
        </h2>
    </div>
    <div style='display: flex; align-items: center; background: rgba(0,255,65,0.1); padding: 5px 15px; border-radius: 20px; border: 1px solid #00FF41;'>
        <div class='pulsing-dot'></div>
        <span class='sys-status'>SYS: SECURE CONNECTION</span>
    </div>
</div>
""", unsafe_allow_html=True)

# ==========================================
# 4. SESSION STATE & CONFIG
# ==========================================
OLLAMA_API_URL = "http://localhost:11434/api/chat"
TARGET_MODEL = "horus-osint"

# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "HORUS SYSTEM INITIALIZED.\n\nAwaiting query parameters... Please input geopolitical or OSINT record queries."}
    ]

# Display chat messages from history on app rerun
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ==========================================
# 5. OLLAMA STREAMING GENERATOR
# ==========================================
def stream_ollama_response(messages):
    """Yields chunks of the response from Ollama API for real-time streaming."""
    payload = {
        "model": TARGET_MODEL,
        "messages": messages,
        "stream": True
    }
    
    try:
        response = requests.post(OLLAMA_API_URL, json=payload, stream=True)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if line:
                chunk = json.loads(line)
                if 'message' in chunk and 'content' in chunk['message']:
                    yield chunk['message']['content']
    except requests.exceptions.ConnectionError:
        yield "\n\n**[CRITICAL ERROR]**: Unable to connect to local Ollama API. Ensure Ollama service is running."
    except Exception as e:
        yield f"\n\n**[SYSTEM FAULT]**: {str(e)}"

# ==========================================
# 6. CHAT INTERFACE & LOGIC
# ==========================================
if prompt := st.chat_input("Enter target parameters or query here..."):
    # Add user message to state and display it
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Display assistant response with streaming effect
    with st.chat_message("assistant"):
        # The st.write_stream function automatically handles the generator
        # and gives the "typing" terminal effect.
        full_response = st.write_stream(stream_ollama_response(st.session_state.messages))
        
    # Add assistant response to chat history
    st.session_state.messages.append({"role": "assistant", "content": full_response})
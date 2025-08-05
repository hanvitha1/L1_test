import os
from flask import Flask, request, redirect, url_for, render_template, session, jsonify, send_file
import hashlib
import uuid
from datetime import datetime, timedelta
import json
import sys
import random
import subprocess
import tempfile
import io
from fpdf import FPDF
import loader
import re
import cv2
import sounddevice as sd
from scipy.io.wavfile import write as write_wav
import numpy as np
import threading
import time
import mss
import mss.tools
import signal
from loader import load_question_bank, get_random_questions

_win32com_client = None
_pythoncom = None
if sys.platform.startswith ( 'win' ):
    try:
        import win32com.client as _win32com_client_import

        _win32com_client = _win32com_client_import
        import pythoncom as _pythoncom_import

        _pythoncom = _pythoncom_import
    except ImportError:
        print ( "WARNING: pywin32 library not found. Email sending via Outlook application will be disabled." )
    except Exception as e:
        print (
            f"WARNING: Error importing pywin32 components: {e}. Email sending via Outlook application will be disabled." )
else:
    print ( "DEBUG: Not on Windows platform. Outlook COM will not be used." )

app = Flask ( __name__ )
app.secret_key = os.urandom ( 24 )

LANGUAGE_CONFIGS = {
    "python": {
        "extension": ".py",
        "run_cmd": ["python"],
        "compile_cmd": []
    },
    "c": {
        "extension": ".c",
        "run_cmd": [],
        # IMPORTANT: Ensure GCC is installed and accessible at this path on your server.
        # Example for Windows with MinGW: r"C:\MinGW\mingw64\bin\gcc.exe"
        # For Linux/macOS, it might just be "gcc" if it's in PATH.
        "compile_cmd": [r"C:\MinGW\mingw64\bin\gcc.exe", "-o"]
    },
    "cpp": {
        "extension": ".cpp",
        "run_cmd": [],
        # IMPORTANT: Ensure G++ is installed and accessible at this path on your server.
        # Example for Windows with MinGW: r"C:\MinGW\mingw64\bin\g++.exe"
        # For Linux/macOS, it might just be "g++" if it's in PATH.
        "compile_cmd": [r"C:\MinGW\mingw64\bin\g++.exe", "-o"]
    }
}

USERS_FILE = 'users.json'
SECURE_LINKS_FILE = 'secure_links.json'
TEST_SESSIONS_FILE = 'test_sessions.json'
PASSWORD_RESET_TOKENS_FILE = 'password_reset_tokens.json'
print (
    f"DEBUG: Data files configured: {USERS_FILE}, {SECURE_LINKS_FILE}, {TEST_SESSIONS_FILE}, {PASSWORD_RESET_TOKENS_FILE}" )

# --- Question Bank Configuration ---
# MODIFIED: Changed to the absolute path provided by the user.
QUESTION_BANK_DIR = r"C:\Users\HanvithaGownipalli\Downloads\mmmmm 4\mmmmm\question_bank"
TOTAL_QUESTIONS_FOR_CANDIDATE_TEST = 5  # Number of questions presented to candidate
TEST_TIME_LIMIT_MINUTES = 60  # Total time limit for the test

# Define the specific categories from which to pick ONE random question each, in this order.
ORDERED_CATEGORIES_FOR_TEST = [
    "A_B",
    "C_D",
    "E_F",
    "G_H",
    "I_J"
]

# Recording parameters (reduced for lower quality/resource usage)
VIDEO_FPS = 10
VIDEO_RESOLUTION = (320, 240)
AUDIO_SAMPLERATE = 22050
AUDIO_CHANNELS = 2
SCREEN_FPS = 10

# Output directory for recordings and FFmpeg path
OUTPUT_DIRECTORY = r"C:\Users\HanvithaGownipalli\Downloads\mmmmm 4\mmmmm\video_folder"
FFMPEG_INSTALL_PATH = r"C:\Users\RajeshThamminaina\Downloads\ffmpeg-7.1.1-full_build\ffmpeg-7.1.1-full_build\bin"

# Global dictionaries to manage active recording threads and their data buffers
active_recording_threads = {}
recording_data_buffers = {}


def sanitize_output(output):
    """
    Removes common input prompts and general descriptive prefixes from the output string,
    then normalizes whitespace for consistent comparison.
    """
    if not output:
        return ""

    # 1. Aggressively remove common input prompts and their variations
    # This list should cover anything that asks for input.
    prompt_patterns = [
        r"Enter\s+a\s+number:\s*",
        r"Enter\s+the\s+number:\s*",
        r"Input\s+value:\s*",
        r"Enter\s+input:\s*",
        r"Please\s+enter\s*.*?:",  # Catches "Please enter X:"
        r"Input\s*:",
        r"Enter\s*:",
        r">>>\s*",  # Python interactive prompt
        r"Enter\s+first\s+number:\s*",  # Specific for "Sum of Two Numbers" C++
        r"Enter\s+second\s+number:\s*",  # Specific for "Sum of Two Numbers" C++
        r"cin\s*>>\s*\w+;",  # C++ cin statements if they appear in output (unlikely but safe)
        r"scanf\s*\(\s*\"[^\"]*\"\s*,\s*&\w+\s*\)\s*;",  # C scanf statements
        r"cout\s*<<\s*\".*?\";",  # C++ cout statements that might be prompts
        r"printf\s*\(\s*\".*?\"\s*\);",  # C printf statements that might be prompts
    ]
    # Combine patterns for global replacement
    combined_prompt_regex = re.compile ( "|".join ( re.escape ( p ) for p in prompt_patterns ), re.IGNORECASE )
    cleaned_output = combined_prompt_regex.sub ( "", output )

    # 2. Remove common descriptive prefixes that might precede the final answer
    # This list should cover phrases like "Sum = ", "Result: ", "X is ".
    descriptive_prefix_patterns = [
        r"^\s*\d+\s+is\s+",  # e.g., "2 is "
        r"^\s*The\s+sum\s+is\s*",
        r"^\s*Sum\s*=\s*",  # Catches "Sum = "
        r"^\s*The\s+product\s+is\s*",
        r"^\s*Result:\s*",
        r"^\s*Output:\s*",
        r"^\s*Your\s+answer:\s*",
        r"^\s*The\s+number\s+is\s*",
        r"^\s*The\s+reversed\s+string\s+is\s*",
        r"^\s*The\s+factorial\s+is\s*",
        r"^\s*Answer:\s*",
        r"^\s*Value:\s*",
        r"^\s*Final\s+Output:\s*",
    ]
    # Combine patterns for global replacement
    combined_prefix_regex = re.compile ( "|".join ( descriptive_prefix_patterns ), re.IGNORECASE )
    extracted_output = combined_prefix_regex.sub ( "", cleaned_output )

    # 3. Normalize all whitespace (multiple spaces/newlines to single space) and strip leading/trailing whitespace.
    normalized_output = re.sub ( r'\s+', ' ', extracted_output ).strip ()

    return normalized_output


def ensure_output_directory():
    """Ensures the output directory for recordings exists."""
    os.makedirs ( OUTPUT_DIRECTORY, exist_ok=True )


def get_base_filename(user_name, session_id, timestamp_str, prefix):
    """Generates a base filename for recording files."""
    return os.path.join ( OUTPUT_DIRECTORY, f"{user_name}_{session_id}_{timestamp_str}_{prefix}" )


def record_webcam(session_id, filename_prefix, recording_duration_seconds, stop_event):
    """Records webcam video."""
    output_filename = f"{filename_prefix}_webcam.mp4"
    cap = cv2.VideoCapture ( 0 )
    if not cap.isOpened ():
        print ( f"Error: Could not open webcam for session {session_id}." )
        stop_event.set ()
        return
    cap.set ( cv2.CAP_PROP_FRAME_WIDTH, VIDEO_RESOLUTION[0] )
    cap.set ( cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_RESOLUTION[1] )

    video_frames = []
    frame_count = 0
    start_time = time.time ()
    try:
        while (time.time () - start_time) < recording_duration_seconds and not stop_event.is_set ():
            ret, frame = cap.read ()
            if not ret:
                print ( f"Warning: Failed to read frame from webcam for session {session_id}." )
                break
            video_frames.append ( frame )
            frame_count += 1
            # Maintain desired FPS
            time_to_sleep = max ( 0, (frame_count / VIDEO_FPS) - (time.time () - start_time) )
            time.sleep ( time_to_sleep )
    except Exception as e:
        print ( f"Error during webcam recording for session {session_id}: {e}" )
    finally:
        cap.release ()
        if video_frames:
            fourcc = cv2.VideoWriter_fourcc ( *'mp4v' )  # Codec for MP4
            out = cv2.VideoWriter ( output_filename, fourcc, VIDEO_FPS, VIDEO_RESOLUTION )
            if not out.isOpened ():
                print ( f"Error: Could not create video writer for webcam output {output_filename}." )
                return
            for frame in video_frames:
                out.write ( frame )
            out.release ()
            print ( f"Webcam recording saved to {output_filename}" )
        else:
            print ( f"No webcam frames captured for session {session_id}." )
        stop_event.set ()  # Ensure stop event is set on completion or error


def record_microphone(session_id, filename_prefix, recording_duration_seconds, stop_event):
    """Records microphone audio."""
    output_filename = f"{filename_prefix}_audio.wav"
    audio_data = []
    try:
        devices = sd.query_devices ()
        input_devices = [d for d in devices if d['max_input_channels'] > 0]
        if not input_devices:
            print ( f"Error: No input audio devices found for session {session_id}." )
            stop_event.set ()
            return

        def callback(indata, frames, time_info, status):
            """Callback function for sounddevice to append audio data."""
            if status:
                print ( f"Audio callback status: {status}" )
            audio_data.append ( indata.copy () )

        with sd.InputStream ( samplerate=AUDIO_SAMPLERATE, channels=AUDIO_CHANNELS, callback=callback,
                              blocksize=1024 ) as stream:
            start_time = time.time ()
            print ( f"Starting audio recording for session {session_id}..." )
            while (time.time () - start_time) < recording_duration_seconds and not stop_event.is_set ():
                time.sleep ( 0.1 )  # Small sleep to prevent busy-waiting
    except sd.PortAudioError as e:
        print ( f"PortAudio Error during microphone recording for session {session_id}: {e}" )
        stop_event.set ()
        return
    except Exception as e:
        print ( f"Error during microphone recording for session {session_id}: {e}" )
        stop_event.set ()
        return
    finally:
        if audio_data:
            try:
                recorded_audio = np.concatenate ( audio_data, axis=0 )
                write_wav ( output_filename, AUDIO_SAMPLERATE, recorded_audio.astype ( np.int16 ) )
                print ( f"Audio recording saved to {output_filename}" )
            except Exception as e:
                print ( f"Error saving audio file for session {session_id}: {e}" )
        else:
            print ( f"No audio data captured for session {session_id}." )
        stop_event.set ()  # Ensure stop event is set on completion or error


def record_screen(session_id, filename_prefix, recording_duration_seconds, stop_event):
    """Records screen video."""
    output_filename = f"{filename_prefix}_screen.mp4"
    screen_frames = []
    sct = mss.mss ()
    # Assuming monitor 1 is the primary display for capture. Adjust if needed.
    monitor_to_capture_index = 1
    try:
        monitor = sct.monitors[monitor_to_capture_index]
    except IndexError:
        print ( f"Error: Monitor {monitor_to_capture_index} not found for screen recording for session {session_id}." )
        stop_event.set ()
        return

    screen_width = monitor["width"]
    screen_height = monitor["height"]
    screen_resolution = (screen_width, screen_height)

    start_time = time.time ()
    frame_count = 0
    try:
        while (time.time () - start_time) < recording_duration_seconds and not stop_event.is_set ():
            sct_img = sct.grab ( monitor )
            frame = np.array ( sct_img )
            frame = cv2.cvtColor ( frame, cv2.COLOR_BGRA2BGR )  # Convert to BGR for OpenCV
            screen_frames.append ( frame )
            frame_count += 1
            # Maintain desired FPS
            time_to_sleep = max ( 0, (frame_count / SCREEN_FPS) - (time.time () - start_time) )
            time.sleep ( time_to_sleep )
    except mss.exception.ScreenShotError as e:
        print ( f"ScreenShotError during screen recording for session {session_id}: {e}" )
        stop_event.set ()
        return
    except Exception as e:
        print ( f"Error during screen recording for session {session_id}: {e}" )
        stop_event.set ()
        return
    finally:
        if screen_frames:
            fourcc = cv2.VideoWriter_fourcc ( *'mp4v' )  # Codec for MP4
            out = cv2.VideoWriter ( output_filename, fourcc, SCREEN_FPS, screen_resolution )
            if not out.isOpened ():
                print ( f"Error: Could not create video writer for screen output {output_filename}." )
                return
            for frame in screen_frames:
                out.write ( frame )
            out.release ()
            print ( f"Screen recording saved to {output_filename}" )
        else:
            print ( f"No screen frames captured for session {session_id}." )
        stop_event.set ()  # Ensure stop event is set on completion or error


def combine_audio_video(video_file, audio_file, output_file, session_id):
    """Combines a video file with an audio file using FFmpeg."""
    ffmpeg_executable = os.path.join ( FFMPEG_INSTALL_PATH, 'ffmpeg.exe' )
    if not os.path.exists ( ffmpeg_executable ):
        print ( f"FFmpeg executable not found at {ffmpeg_executable}. Cannot combine files." )
        return False
    try:
        command = [
            ffmpeg_executable,
            '-i', video_file,
            '-i', audio_file,
            '-c:v', 'libx264',  # Use libx264 for H.264 video encoding
            '-preset', 'fast',  # Faster encoding, slightly larger file
            '-crf', '28',  # Higher CRF for lower quality (more compression)
            '-b:v', '500k',  # Target video bitrate for lower quality
            '-c:a', 'aac',
            '-b:a', '64k',  # Target audio bitrate for lower quality
            '-strict', 'experimental',  # Needed for aac on some FFmpeg versions
            '-map', '0:v:0',  # Map video stream from first input
            '-map', '1:a:0',  # Map audio stream from second input
            '-y',  # Overwrite output file if it exists
            output_file
        ]
        print ( f"Combining {video_file} and {audio_file} into {output_file}..." )
        subprocess.run ( command, check=True, capture_output=True, text=True )
        print ( f"Successfully combined files for session {session_id}." )
        return True
    except subprocess.CalledProcessError as e:
        print ( f"FFmpeg combination error for session {session_id}. Stdout: {e.stdout}, Stderr: {e.stderr}" )
        return False
    except FileNotFoundError:
        print ( f"FFmpeg executable not found at {ffmpeg_executable}." )
        return False
    except Exception as e:
        print ( f"Unexpected error during FFmpeg combination for session {session_id}: {e}" )
        return False


def cleanup_temp_files(session_id, *filepaths):
    """Removes temporary recording files."""
    print ( f"Cleaning up temporary files for session {session_id}..." )
    for f_path in filepaths:
        if os.path.exists ( f_path ):
            try:
                os.remove ( f_path )
                print ( f"Removed temporary file: {f_path}" )
            except OSError as e:
                print ( f"Error removing temporary file {f_path}: {e}" )


def load_data(filename):
    if os.path.exists ( filename ):
        with open ( filename, 'r', encoding='utf-8' ) as f:
            try:
                data = json.load ( f )
                if filename == SECURE_LINKS_FILE or filename == PASSWORD_RESET_TOKENS_FILE:
                    for token, link_data in data.items ():
                        if 'expires_at' in link_data and isinstance ( link_data['expires_at'], str ):
                            link_data['expires_at'] = datetime.fromisoformat ( link_data['expires_at'] )
                elif filename == TEST_SESSIONS_FILE:
                    for session_id, session_data in data.items ():
                        if 'start_time' in session_data and isinstance ( session_data['start_time'], str ):
                            session_data['start_time'] = datetime.fromisoformat ( session_data['start_time'] )
                        if 'end_time' in session_data and isinstance ( session_data['end_time'], str ):
                            session_data['end_time'] = datetime.fromisoformat ( session_data['end_time'] )
                        if 'test_questions_order' in session_data and all (
                                isinstance ( q_id, str ) for q_id in session_data['test_questions_order'] ):
                            pass  # Keep as IDs for now, resolve when needed for display
                        if 'answers' in session_data:
                            for q_id, answer in session_data['answers'].items ():
                                if 'submission_time' in answer and isinstance ( answer['submission_time'], str ):
                                    answer['submission_time'] = datetime.fromisoformat ( answer['submission_time'] )
                return data
            except json.JSONDecodeError as e:
                timestamp = datetime.now ().strftime ( "%Y%m%d_%H%M%S" )
                backup_filename = f"{filename}.corrupted_backup_{timestamp}"
                try:
                    import shutil
                    shutil.copy2 ( filename, backup_filename )  # Copy preserves metadata
                except Exception as backup_e:
                    print ( f"--- ERROR --- Failed to create backup of {filename}: {backup_e}" )
                return {}
            except Exception as e:
                return {}
    return {}


# Load initial data from JSON files
users = load_data ( USERS_FILE )
secure_links = load_data ( SECURE_LINKS_FILE )
test_sessions = load_data ( TEST_SESSIONS_FILE )
password_reset_tokens = load_data ( PASSWORD_RESET_TOKENS_FILE )


def save_data(data, filename):
    with open ( filename, 'w', encoding='utf-8' ) as f:
        if filename == SECURE_LINKS_FILE or filename == PASSWORD_RESET_TOKENS_FILE:
            serializable_data = {}
            for token, link_data in data.items ():
                temp_data = link_data.copy ()
                if 'expires_at' in temp_data and isinstance ( temp_data['expires_at'], datetime ):
                    temp_data['expires_at'] = temp_data['expires_at'].isoformat ()
                serializable_data[token] = temp_data
            json.dump ( serializable_data, f, indent=4 )
        elif filename == TEST_SESSIONS_FILE:
            serializable_data = {}
            for session_id, session_data in data.items ():
                temp_data = session_data.copy ()
                if 'start_time' in temp_data and isinstance ( temp_data['start_time'], datetime ):
                    temp_data['start_time'] = temp_data['start_time'].isoformat ()
                if 'end_time' in temp_data and isinstance ( temp_data['end_time'], datetime ):
                    temp_data['end_time'] = temp_data['end_time'].isoformat ()
                if 'answers' in temp_data:
                    for q_id, answer in temp_data['answers'].items ():
                        if 'submission_time' in answer and isinstance ( answer['submission_time'], datetime ):
                            answer['submission_time'] = answer['submission_time'].isoformat ()
                serializable_data[session_id] = temp_data
            json.dump ( serializable_data, f, indent=4 )
        else:
            json.dump ( data, f, indent=4 )


def get_all_questions_flattened(question_bank_dict):
    all_q = []
    for category_key in question_bank_dict.keys ():
        all_q.extend ( question_bank_dict[category_key] )
    return all_q


# Removed create_dummy_question_bank() as per user request


ADMIN_SENDER_EMAIL = 'rajesh.thamminaina@lyptus-tech.com'
OUTLOOK_DOMAINS = ['outlook.com', 'gmail.com', 'hotmail.com', 'live.com', 'msn.com', 'lyptus-tech.com']
print ( f"DEBUG: Admin sender email: {ADMIN_SENDER_EMAIL}" )


def hash_password(password):
    return hashlib.sha256 ( password.encode () ).hexdigest ()


def check_password(hashed_password, password):
    return hashed_password == hash_password ( password )


def is_allowed_email_domain(email):
    if '@' in email:
        domain = email.split ( '@' )[1].lower ()
        is_allowed = domain in OUTLOOK_DOMAINS
        return is_allowed
    return False


def get_client_info():
    ip_address = request.remote_addr
    user_agent = request.headers.get ( 'User-Agent' )
    return ip_address, user_agent


def send_outlook_email(to_email, subject, body, html=True, cc_email=None):
    if not sys.platform.startswith ( 'win' ):
        if cc_email:
            print ( f"📬 CC: {cc_email}" )
        return True  # Simulate success for non-Windows environments

    if _win32com_client is None or _pythoncom is None:
        if cc_email:
            print ( f"📬 CC: {cc_email}" )
        return False
    try:
        _pythoncom.CoInitialize ()
        outlook = _win32com_client.Dispatch ( 'Outlook.Application' )
        mail = outlook.CreateItem ( 0 )
        mail.To = to_email
        if cc_email:
            mail.CC = cc_email
        mail.Subject = subject
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
        mail.Send ()
        print ( f"✅ Email sent via Outlook to {to_email}" )
        return True
    except Exception as e:
        print ( f"❌ Error sending email to {to_email} via Outlook COM: {str ( e )}" )
        print ( f"📬 To: {to_email}" )
        if cc_email:
            print ( f"📬 CC: {cc_email}" )
        return False
    finally:
        if sys.platform.startswith ( 'win' ):
            _pythoncom.CoUninitialize ()


def is_placeholder_code(code_string, language="python"):
    """
    Checks if the given code string is likely a placeholder or empty code.
    This function is now more robust to allow comments.
    """
    if not code_string or not code_string.strip ():
        return True

    normalized_code = code_string.strip ()

    # Remove all comments for the purpose of checking if there's actual executable code
    if language == "python":
        # Remove single-line comments
        normalized_code = re.sub ( r'#.*', '', normalized_code )
        # Remove multi-line string literals that might be used as docstrings/comments
        normalized_code = re.sub ( r'("""[^"]*"""|\'\'\'[^\']*\'\'\')', '', normalized_code, flags=re.DOTALL )
    elif language in ["c", "cpp"]:
        # Remove single-line comments //
        normalized_code = re.sub ( r'//.*', '', normalized_code )
        # Remove multi-line comments /* ... */
        normalized_code = re.sub ( r'/\*.*?\*/', '', normalized_code, flags=re.DOTALL )

    # Remove all whitespace (spaces, tabs, newlines)
    normalized_code_stripped = re.sub ( r'\s+', '', normalized_code )

    if not normalized_code_stripped:
        return True  # Only comments or whitespace left

    # Check for common empty starter code patterns after stripping comments
    if language == "python":
        if normalized_code_stripped in ["print()", "return"]:
            return True
    elif language in ["c", "cpp"]:
        empty_c_main = "intmain(){return0;}"
        empty_cpp_main_cout = "intmain(){std::cout<<std::endl;return0;}"
        empty_cpp_main = "intmain(){return0;}"
        if normalized_code_stripped in [empty_c_main, empty_cpp_main_cout, empty_cpp_main]:
            return True

    # Check for specific placeholder phrases
    if "no specific starter code" in normalized_code.lower () and "please write your solution here" in normalized_code.lower ():
        return True

    # Check against dummy starter codes (after stripping comments and whitespace)
    # This part needs to be careful since create_dummy_question_bank is removed.
    # It should only check against actual loaded questions.
    # For now, we'll keep it as is, assuming full_question_bank is loaded from JSON files.
    dummy_starter_codes_stripped = []
    # Ensure full_question_bank is accessible here. It's a global, so it should be.
    # However, if this function is called before full_question_bank is fully loaded, it might be empty.
    # This is a potential point of failure.
    for q in get_all_questions_flattened ( full_question_bank ):
        # Determine the languages data structure (list or dict)
        languages_data = q.get ( 'languages' )
        if isinstance ( languages_data, dict ):
            # New format: iterate through language keys in the dictionary
            for lang_key, lang_data in languages_data.items ():
                if lang_data.get ( 'starter_code' ):
                    stripped_starter = re.sub ( r'#.*', '', lang_data['starter_code'] )  # Python comments
                    stripped_starter = re.sub ( r'//.*', '', stripped_starter )  # C/C++ single-line comments
                    stripped_starter = re.sub ( r'/\*.*?\*/', '', stripped_starter,
                                                flags=re.DOTALL )  # C/C++ multi-line comments
                    dummy_starter_codes_stripped.append ( re.sub ( r'\s+', '', stripped_starter ) )
        elif isinstance ( languages_data, list ):
            # Old format: languages is a list of strings, so no starter_code directly in JSON.
            # We'll generate generic placeholders for comparison if needed.
            for lang_key in languages_data:
                comment_symbol = '#' if lang_key == 'python' else '//'
                generic_starter = f'{comment_symbol} No specific starter code for {lang_key}. Please write your solution here.'
                dummy_starter_codes_stripped.append ( re.sub ( r'\s+', '', generic_starter ) )

    if normalized_code_stripped in dummy_starter_codes_stripped:
        return True

    return False


def clean_traceback_error(error_msg):
    # Strip file paths from traceback lines
    cleaned_lines = []
    for line in error_msg.splitlines ():
        # Remove lines containing file paths (e.g., "File "..."")
        if "File " in line or "at " in line:  # Also catch "at <path>" common in some runtime errors
            continue
        # Remove common compiler warnings/notes that include paths or are less critical
        if re.search ( r'(?:[a-zA-Z]:)?[\\/][^:\n]+\.(c|cpp|py)(:\d+)?', line ):
            continue  # Remove lines that still contain file paths even if not "File "
        if "note:" in line.lower () or "warning:" in line.lower ():
            continue
        cleaned_lines.append ( line )
    return "\n".join ( cleaned_lines ).strip ()


def execute_code_with_subprocess(code, language, input_data="", timeout=10):
    if is_placeholder_code ( code, language ):
        return "", "No valid code submitted or placeholder code detected. Please write your solution.", False

    lang_config = LANGUAGE_CONFIGS.get ( language )
    if not lang_config:
        return "", f"Unsupported language: {language}", False

    with tempfile.TemporaryDirectory () as tmpdir:
        unique_id = str ( uuid.uuid4 () )
        code_file_path = os.path.join ( tmpdir, f"{unique_id}{lang_config['extension']}" )
        executable_file_path = os.path.join ( tmpdir, f"{unique_id}.out" )

        with open ( code_file_path, "w", encoding='utf-8' ) as f:
            f.write ( code )

        # Compile for C/C++
        if language in ["c", "cpp"]:
            compile_command = lang_config["compile_cmd"] + [executable_file_path, code_file_path]
            try:
                compile_result = subprocess.run (
                    compile_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                    check=False,
                    env=os.environ,
                    cwd=tmpdir
                )
                if compile_result.returncode != 0:
                    error_msg = compile_result.stderr.strip ()
                    # Clean compilation error messages
                    cleaned_error_msg = clean_traceback_error ( error_msg )
                    return "", f"Compilation Error:\n{cleaned_error_msg}", False
            except FileNotFoundError:
                return "", f"Compiler for {language} not found. Please ensure {language} compiler (e.g., GCC/G++) is installed and correctly configured in LANGUAGE_CONFIGS.", False
            except Exception as e:
                return "", f"Unexpected compilation error: {str ( e )}", False

        # Execution command
        execute_cmd = []
        if language == "python":
            execute_cmd = lang_config["run_cmd"] + [code_file_path]
        else:
            execute_cmd = [executable_file_path]

        try:
            exec_result = subprocess.run (
                execute_cmd,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                cwd=tmpdir
            )
            output = exec_result.stdout.strip ()
            error = exec_result.stderr.strip ()

            # Clean runtime error messages
            cleaned_error = clean_traceback_error ( error )

            return output, cleaned_error, True
        except subprocess.TimeoutExpired:
            return "", "Execution timed out", False
        except FileNotFoundError:
            return "", f"Executable for {language} not found. This might indicate a compilation issue or missing runtime.", False
        except Exception as e:
            # Catch any other unexpected runtime errors
            return "", f"Runtime error: {clean_traceback_error ( str ( e ) )}", False


full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )  # Load question bank early
users = load_data ( USERS_FILE )
secure_links = load_data ( SECURE_LINKS_FILE )
test_sessions = load_data ( TEST_SESSIONS_FILE )
password_reset_tokens = load_data ( PASSWORD_RESET_TOKENS_FILE )


@app.route ( '/' )
@app.route ( '/login', methods=['GET', 'POST'] )
def login():
    if request.method == 'POST':
        username_or_email = request.form['username_or_email'].lower ()
        password = request.form['password']
        if username_or_email == ADMIN_SENDER_EMAIL and password == 'password123':
            session['logged_in'] = True
            session['user_email'] = username_or_email
            session['user_role'] = 'admin'
            session['user_fullname'] = 'Admin'
            print ( f"DEBUG: Admin '{username_or_email}' logged in successfully." )
            return redirect ( url_for ( 'dashboard', user=username_or_email ) )
        if username_or_email in users and check_password ( users[username_or_email]['password_hash'], password ):
            session['logged_in'] = True
            session['user_email'] = username_or_email
            session['user_role'] = users[username_or_email].get ( 'role', 'reviewer' )
            session['user_fullname'] = users[username_or_email].get ( 'fullname', 'User' )
            print ( f"DEBUG: User '{username_or_email}' ({session['user_role']}) logged in successfully." )
            return redirect ( url_for ( 'dashboard', user=username_or_email ) )
        else:
            error_message = "Invalid username/email or password."
            print ( f"DEBUG: Login failed for '{username_or_email}': {error_message}" )
            return redirect ( url_for ( 'login', error=error_message ) )
    return render_template ( 'login.html' )


def get_user_display_name(email):
    users = load_data ( USERS_FILE )
    user = users.get ( email )
    if user:
        name = user.get ( "fullname" ) or user.get ( "name" )
        if name:
            # Capitalize each word
            return " ".join ( part.capitalize () for part in name.split () )
    return email  # fallback


@app.route ( '/dashboard', methods=['GET', 'POST'] )
def dashboard():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect ( url_for ( 'login', error="Please log in to access the dashboard." ) )
    reviewer_email = session['user_email']
    reviewer_name = get_user_display_name ( reviewer_email )
    message = None
    is_admin = (reviewer_email == ADMIN_SENDER_EMAIL)
    current_user_profile_data = {
        'fullname': users.get ( reviewer_email, {} ).get ( 'fullname', 'N/A' ),
        'email': reviewer_email,
        'role': session.get ( 'user_role', 'reviewer' )
    }
    non_admin_users_count = sum ( 1 for email, user_info in users.items () if email != ADMIN_SENDER_EMAIL )
    admin_is_only_user = (len ( users ) == 1 and ADMIN_SENDER_EMAIL in users and users[ADMIN_SENDER_EMAIL].get (
        'role' ) == 'admin') or (non_admin_users_count == 0)
    if request.method == 'POST' and not is_admin:
        recipient_emails_str = request.form.get ( 'recipient_emails', '' )
        recipient_emails = [e.strip ().lower () for e in recipient_emails_str.split ( ',' ) if e.strip ()]
        if not recipient_emails:
            message = {"type": "error", "text": "Please enter at least one recipient email address."}
        else:
            successful_emails = []
            failed_emails = []
            for email in recipient_emails:
                if not is_allowed_email_domain ( email ):
                    failed_emails.append ( email )
                    continue
                token = str ( uuid.uuid4 () )
                # MODIFIED: Link expiration changed from 7 days to 15 hours. days=7
                expires_at = datetime.now () + timedelta ( days=7 )
                secure_links[token] = {
                    'email': email,
                    'reviewer_email': reviewer_email,
                    'expires_at': expires_at,
                    'first_access_ip': None,
                    'first_access_ua': None,
                    'activated': False,
                    'candidate_registered_id': None
                }
                secure_url = url_for ( 'secure_access', token=token, _external=True )
                email_subject = "Your Secure Qualification Test Link"
                email_body = f"""
                <html>
                  <body style="font-family: Arial, sans-serif; color: #333;">
                    <p>Dear Candidate,</p>

                    <p>
                      A secure test link has been generated for you by <strong>{reviewer_name}</strong>.
                    </p>
                    <p>Please use the following button to access your registration page and start the test:</p>
                    <div style="margin: 24px 0;">
                      <a href="{secure_url}" style="
                          display: inline-block; /* Changed to inline-block for broader compatibility */
                          padding: 12px 24px;
                          font-size: 18px;
                          font-weight: 600;
                          border-radius: 1rem;
                          color: #ffffff;
                          background-color: #14b8a6;
                          text-decoration: none;
                          box-shadow: 0 6px 12px rgba(0, 0, 0, 0.1);
                          font-family: Arial, sans-serif;
                          vertical-align: middle; /* Helps align text and emoji vertically */
                      ">
                        🚀 Click Here to Start Test
                      </a>
                    </div>
                    <p style="margin-top: 20px;">
                      This link is valid for <strong>15 hours</strong> and can only be used once, from the <strong>first device</strong> that accesses it.
                    </p>
                    <p> before start the test follow the <strong> Important Instructions for the Test </strong> </p>
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #ccc;" />
                    <h3 style="color: #0f172a;">📝 Important Instructions for the Test</h3>
                    <ul style="margin-left: 20px;">
                      <li>You must use a <strong>Windows laptop or desktop</strong> for this test.</li>
                      <li>A stable and fast <strong>Wi-Fi internet connection</strong> is mandatory.</li>
                      <li>Your system must have a functioning <strong>webcam, microphone, and screen sharing</strong>.</li>
                      <li>You must <strong>allow all permissions</strong> (camera, mic, screen) when prompted.</li>
                      <li>Once started, the test is time-limited and <strong>cannot be paused or restarted</strong>.</li>
                      <li>Recording (webcam, audio, and screen) will run throughout the test for monitoring purposes.</li>
                      <li>Use of mobile devices, tablets, or incognito/private browsing is <strong>strictly prohibited</strong>.</li>
                      <li>Please close all other applications before starting.</li>
                    </ul>
                    <p style="margin-top: 20px; font-weight: bold; color: #dc2626;">
                      Failure to follow these instructions may result in test disqualification or incomplete submissions.
                    </p>
                    <p><strong>Good luck!</strong></p>
                    <hr style="margin: 30px 0; border: none; border-top: 1px solid #ccc;" />
                    <p>
                      <strong>Regards,<br>Lyptus - Recruitment Analytics</strong>
                    </p>
                    <p style="font-size: 12px; color: #777;">
                      This is an auto-generated mail. Please do not reply.
                    </p>
                  </body>
                </html>
                """
                if send_outlook_email ( email, email_subject, email_body ):
                    successful_emails.append ( email )
                else:
                    failed_emails.append ( email )
                    secure_links.pop ( token, None )  # Remove link if email failed to send
            save_data ( secure_links, SECURE_LINKS_FILE )
            if successful_emails:
                message = {"type": "success",
                           "text": f"Links generated and emails sent to: {', '.join ( successful_emails )}."}
            if failed_emails:
                error_part = f" Failed to generate/send links for: {', '.join ( failed_emails )} (invalid email domain or email sending failed. Check console for details)."
                if message and message["type"] == "success":
                    message["text"] += error_part
                else:
                    message = {"type": "error", "text": error_part.strip ()}
            if not successful_emails and not failed_emails:
                message = {"type": "error", "text": "No valid email addresses provided for link generation."}
    elif request.method == 'POST' and is_admin:
        message = {"type": "error", "text": "Admins cannot generate links from this dashboard directly."}
    all_links_with_details = []
    sorted_secure_links = sorted ( secure_links.items (), key=lambda item: item[1]['expires_at'], reverse=True )
    for token, link_data in sorted_secure_links:
        link_details = link_data.copy ()
        link_details['token'] = token
        link_details['url'] = url_for ( 'secure_access', token=token, _external=True )
        link_details['is_activated'] = bool ( link_data.get ( 'activated' ) )
        link_details['is_expired'] = datetime.now () > link_data['expires_at']
        if link_details['is_expired']:
            link_details['status'] = "Expired (Unused Link)"
        elif link_details['is_activated']:
            if link_data.get ( 'candidate_registered_id' ):
                candidate_session = test_sessions.get ( link_data['candidate_registered_id'] )
                if candidate_session and candidate_session.get ( 'is_completed' ):
                    link_details['status'] = "Test Completed"
                elif candidate_session and not candidate_session.get ( 'is_completed' ):
                    link_details['status'] = "Test Started (In Progress)"
                else:
                    link_details['status'] = "Activated (No Test Session Found)"
            else:
                link_details['status'] = "Activated (No Registration Yet)"
        else:
            link_details['status'] = "Pending Activation"
        # Modified line to explicitly show "Expires: "
        link_details['expires_at_str'] = "Expires: " + link_data['expires_at'].strftime ( '%Y-%m-%d %H:%M:%S' )
        registered_candidate_info = None
        if link_data.get ( 'candidate_registered_id' ):
            candidate_session_id = link_data['candidate_registered_id']
            candidate_session = test_sessions.get ( candidate_session_id )
            if candidate_session:
                registered_candidate_info = {
                    'full_name': candidate_session.get ( 'full_name', 'N/A' ),
                    'email': candidate_session.get ( 'email', 'N/A' ),
                    'phone_number': candidate_session.get ( 'phone_number', 'N/A' ),
                    'test_started_at': candidate_session['start_time'].strftime ( '%Y-%m-%d %H:%M:%S' ) if isinstance (
                        candidate_session.get ( 'start_time' ), datetime ) else 'N/A',
                    'test_completed': candidate_session.get ( 'is_completed', False ),
                    'score': candidate_session.get ( 'score', 'N/A' ),
                    'candidate_user_id': candidate_session_id}
        link_details['registered_candidate'] = registered_candidate_info
        all_links_with_details.append ( link_details )
    global full_question_bank
    full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )
    users_for_display = {}
    for email, user_data in users.items ():
        users_for_display[email] = user_data.copy ()
        users_for_display[email].pop ( 'password_hash', None )
        if 'role' not in users_for_display[email]:
            users_for_display[email]['role'] = 'reviewer'
    current_view = request.args.get ( 'view' )
    if current_view is None:
        current_view = 'profile'
    return render_template ( 'dashboard.html',
                             user_email=reviewer_email,
                             generated_links_data=all_links_with_details,
                             message=request.args.to_dict () if request.args else (message if message else {}),
                             is_admin=is_admin,
                             all_users=users_for_display,
                             admin_is_only_user=admin_is_only_user,
                             full_question_bank_for_display=full_question_bank,
                             current_view=current_view,
                             current_user_profile=current_user_profile_data )


@app.route ( '/admin/links/delete/<link_token>', methods=['POST'] )
def delete_secure_link(link_token):
    global secure_links
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401
    if link_token in secure_links:
        del secure_links[link_token]
        save_data ( secure_links, SECURE_LINKS_FILE )
        return jsonify ( {"message": f"Link '{link_token}' deleted successfully.", "type": "success"} ), 200
    else:
        return jsonify ( {"message": "Link not found.", "type": "error"} ), 404


@app.route ( "/start_recording", methods=["POST"] )
def start_recording():
    data = request.get_json ()
    candidate_user_id = data.get ( "candidate_user_id" )
    duration = data.get ( "test_duration_minutes", 60 )
    print ( f"📹 Starting recording for user {candidate_user_id}, duration: {duration} min" )
    return jsonify ( {"message": "Recording initialized"} ), 200


# @app.route ( '/upload_recordings', methods=['POST'] )
# def upload_recordings():
#     candidate_id = request.form.get ( "candidate_id" )
#     webcam = request.files.get ( "webcam" )
#     screen = request.files.get ( "screen" )
#
#     os.makedirs ( f"recordings/{candidate_id}", exist_ok=True )
#     if webcam:
#         webcam.save ( f"recordings/{candidate_id}/webcam.webm" )
#     if screen:
#         screen.save ( f"recordings/{candidate_id}/screen.webm" )
#
#     return jsonify ( {"status": "success"} ), 200

@app.route('/upload_recordings', methods=['POST'])
def upload_recordings():
    candidate_id = request.form.get("candidate_id")
    webcam = request.files.get("webcam")
    screen = request.files.get("screen")

    if not candidate_id:
        return jsonify({"error": "Missing candidate_id"}), 400

    # Create folder if not exists
    folder = os.path.join("recordings", candidate_id)
    os.makedirs(folder, exist_ok=True)

    # Determine filename based on whether it's final or early
    suffix = "_early" if "early" in request.form else ""

    if webcam:
        webcam_path = os.path.join(folder, f"webcam{suffix}.webm")
        webcam.save(webcam_path)
        print(f"✅ Saved webcam to: {webcam_path}")
    if screen:
        screen_path = os.path.join(folder, f"screen{suffix}.webm")
        screen.save(screen_path)
        print(f"✅ Saved screen to: {screen_path}")

    return jsonify({"status": "success"}), 200

@app.route ( '/secure_access/<token>' )
def secure_access(token):
    link_data = secure_links.get ( token )

    full_name = 'Candidate'
    email = 'N/A'
    answered_count = 0

    # Try to find associated test session to get candidate details and answered count
    associated_session = None
    for sid, sdata in test_sessions.items ():
        if sdata.get ( 'link_token' ) == token:
            associated_session = sdata
            full_name = sdata.get ( 'candidate_full_name', full_name )
            email = sdata.get ( 'candidate_email', email )
            answered_count = len ( sdata.get ( 'answers', {} ) )
            break

    if not link_data:
        return render_template ( 'link_expired.html', message="Invalid or non-existent secure link." )
    if datetime.now () > link_data['expires_at']:
        secure_links.pop ( token, None )
        save_data ( secure_links, SECURE_LINKS_FILE )
        return render_template ( 'link_expired.html', message="This link has expired and is no longer valid." )
    current_ip, current_ua = get_client_info ()
    if not link_data['activated']:
        link_data['first_access_ip'] = current_ip
        link_data['first_access_ua'] = current_ua
        link_data['activated'] = True
        secure_links[token] = link_data
        save_data ( secure_links, SECURE_LINKS_FILE )
        return redirect ( url_for ( 'test_registration_page', link_hash=token ) )
    candidate_user_id_for_redirect = link_data.get ( 'candidate_registered_id' )
    if candidate_user_id_for_redirect:
        existing_session = test_sessions.get ( candidate_user_id_for_redirect )
        if existing_session:
            if existing_session.get ( 'is_completed' ):
                return render_template ( 'link_expired.html',
                                         message="This test link has already been used and completed. Please contact your reviewer if you believe this is an error." )
            elif existing_session.get ( 'status' ) == 'Expired or Force Closed':
                return render_template ( 'link_expired.html',
                                         message="This test session was force-closed or expired due to refresh or tab close. Please contact your reviewer." )
            else:
                existing_candidate_token = existing_session.get ( 'token' )
                if existing_candidate_token:
                    return redirect ( url_for ( 'test_page', candidate_user_id=candidate_user_id_for_redirect,
                                                candidate_token=existing_candidate_token ) )
                else:
                    return render_template ( 'link_expired.html',
                                             message="Your session data is incomplete. Please request a new link." )
        else:
            return render_template ( 'link_expired.html',
                                     message="Test session not found. This link might have been used or is invalid." )
    return redirect ( url_for ( 'test_registration_page', link_hash=token,
                                message="This link has been activated. Please complete your registration to start the test." ) )


@app.route ( '/secure_link_page.html' )
def secure_link_page_route():
    message = request.args.get ( 'message', 'This link is no longer valid or has already been used.' )
    return render_template ( 'secure_link_page.html', message=message )


@app.route ( '/stop_recording', methods=['POST'] )
def stop_recording():
    data = request.get_json ()
    candidate_user_id = data.get ( 'candidate_user_id' )

    # You can add logic here to stop/save the recording, or simulate a response
    print ( f"🛑 Stop recording request for user: {candidate_user_id}" )

    return jsonify ( {'message': f'Recording stopped for user {candidate_user_id}'} ), 200


@app.route ( '/link_expired' )
def link_expired():
    message = request.args.get ( 'message', 'This link is no longer valid or has expired.' )
    return render_template ( 'link_expired.html', message=message )


@app.route ( '/test_registration_page' )
def test_registration_page():
    link_hash = request.args.get ( 'link_hash' )
    message = request.args.get ( 'message' )
    if not link_hash or link_hash not in secure_links:
        return redirect ( url_for ( 'link_expired', message="Invalid test registration link." ) )
    link_data = secure_links[link_hash]
    if link_data.get ( 'candidate_registered_id' ):
        existing_candidate_id = link_data['candidate_registered_id']
        candidate_session = test_sessions.get ( existing_candidate_id )
        if candidate_session:
            if candidate_session['is_completed']:
                return redirect ( url_for ( 'submitted_page',
                                            name=candidate_session.get ( 'full_name', 'Candidate' ),
                                            email=candidate_session.get ( 'email', 'N/A' ),
                                            answered_count=len ( candidate_session.get ( 'answers', {} ) ) ) )
            else:
                return redirect ( url_for ( 'test_page', candidate_user_id=existing_candidate_id,
                                            candidate_token=test_sessions[existing_candidate_id]['token'] ) )
        else:
            return redirect ( url_for ( 'link_expired',
                                        message="Your previous test session data was not found. Please request a new link." ) )
    return render_template ( 'test_registration_page.html', link_hash=link_hash, message=message )


@app.route ( '/api/candidates/test/register_and_start/<link_hash>', methods=['POST'] )
def register_and_start_test(link_hash):
    data = request.get_json ()
    full_name = data.get ( 'full_name' )
    email = data.get ( 'email' ).lower ()
    phone_number = data.get ( 'phone_number' )
    link_data = secure_links.get ( link_hash )
    if not link_data:
        return jsonify ( {"message": "Invalid or expired secure link."} ), 400
    if not is_allowed_email_domain ( email ):
        return jsonify (
            {"message": f"Please use an email address from allowed domains: {', '.join ( OUTLOOK_DOMAINS )}."} ), 400
    if link_data.get ( 'candidate_registered_id' ):
        existing_candidate_id = link_data['candidate_registered_id']
        candidate_session = test_sessions.get ( existing_candidate_id )
        if candidate_session:
            if candidate_session.get ( 'is_completed' ):
                return jsonify ( {"message": "You have already completed this test. This link is no longer active.",
                                  "test_url": url_for ( 'submitted_page',
                                                        name=candidate_session.get ( 'full_name', 'Candidate' ),
                                                        email=candidate_session.get ( 'email', 'N/A' ),
                                                        answered_count=len (
                                                            candidate_session.get ( 'answers', {} ) ) ),
                                  "completed": True} ), 200
            candidate_session['full_name'] = full_name
            candidate_session['email'] = email
            candidate_session['phone_number'] = phone_number
            save_data ( test_sessions, TEST_SESSIONS_FILE )
            return jsonify ( {
                "message": "You are already registered for this test. Redirecting to test page.",
                "candidate_token": candidate_session['token'],
                "user_id_for_test": existing_candidate_id,
                "time_limit_minutes": candidate_session['time_limit_minutes'],
                "test_url": url_for ( 'test_page', candidate_user_id=existing_candidate_id,
                                      candidate_token=candidate_session['token'] )
            } ), 200
        else:
            link_data['candidate_registered_id'] = None
            save_data ( secure_links, SECURE_LINKS_FILE )

    candidate_user_id = str ( uuid.uuid4 () )
    candidate_token = str ( uuid.uuid4 () )

    # --- NEW QUESTION SELECTION LOGIC ---
    # Ensure full_question_bank is loaded before selection
    global full_question_bank  # Declare global to ensure we are using the global variable
    full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )

    selected_questions_for_test = []
    for category_key in ORDERED_CATEGORIES_FOR_TEST:
        # Get one random question from the current category using loader.get_random_questions
        # This function already handles picking a random sample.
        questions_from_category = loader.get_random_questions ( full_question_bank, category_key, 1 )

        if not questions_from_category:
            return jsonify ( {
                "message": f"Error: No questions found or not enough questions in category '{category_key}' to select a random one. Please contact administrator."
            } ), 500

        # Add the single random question to the list
        selected_questions_for_test.append ( questions_from_category[0] )

    # Ensure the total number of questions matches the expected count
    if len ( selected_questions_for_test ) != len ( ORDERED_CATEGORIES_FOR_TEST ):
        return jsonify ( {
            "message": "Error: Could not select the required number of questions from all categories. Please check question bank configuration."
        } ), 500
    # --- END NEW QUESTION SELECTION LOGIC ---

    test_sessions[candidate_user_id] = {
        'full_name': full_name,
        'email': email,
        'phone_number': phone_number,
        'link_hash_used': link_hash,
        'start_time': datetime.now (),
        'time_limit_minutes': TEST_TIME_LIMIT_MINUTES,
        'current_question_index': 0,
        'answers': {},
        'token': candidate_token,
        'is_completed': False,
        'score': None,
        'test_questions_order': [q['id'] for q in selected_questions_for_test]
    }
    save_data ( test_sessions, TEST_SESSIONS_FILE )
    secure_links[link_hash]['candidate_registered_id'] = candidate_user_id
    save_data ( secure_links, SECURE_LINKS_FILE )

    print (
        f"DEBUG: Test session created for {candidate_user_id}. Questions order: {test_sessions[candidate_user_id]['test_questions_order']}" )

    return jsonify ( {
        "message": "Registration successful! Redirecting to test page.",
        "candidate_token": candidate_token,
        "user_id_for_test": candidate_user_id,
        "time_limit_minutes": TEST_TIME_LIMIT_MINUTES,
        "test_url": url_for ( 'test_page', candidate_user_id=candidate_user_id,
                              candidate_token=candidate_token )} ), 200


@app.route ( '/test_page/<candidate_user_id>/<candidate_token>' )
def test_page(candidate_user_id, candidate_token):
    return render_template ( 'test_page.html', candidate_user_id=candidate_user_id )


@app.route ( '/api/candidates/test/current_question/<candidate_user_id>', methods=['GET'] )
def get_current_question(candidate_user_id):
    print ( f"DEBUG: get_current_question API called for candidate_user_id: {candidate_user_id}" )
    candidate_session = test_sessions.get ( candidate_user_id )
    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header:
        auth_token = auth_token_header.split ( "Bearer " )[1]
    expected_token = candidate_session.get ( 'token' ) if candidate_session else None
    if not candidate_session or auth_token != expected_token:
        print ( f"DEBUG: Unauthorized access for {candidate_user_id}. Token mismatch or session not found." )
        return jsonify ( {"message": "Unauthorized or session expired. Please re-register via your secure link."} ), 401
    if candidate_session.get ( 'is_completed' ):
        print ( f"DEBUG: Test for {candidate_user_id} already completed. Redirecting to results." )
        return jsonify ( {
            "message": "Test already completed. Redirecting to results.",
            "completed": True,
            "status": "completed"
        } ), 200

    current_q_index = candidate_session['current_question_index']
    test_question_ids = candidate_session.get ( 'test_questions_order', [] )

    print ( f"DEBUG: get_current_question for {candidate_user_id}. Current index: {current_q_index}" )
    print ( f"DEBUG: test_questions_order for session: {test_question_ids}" )

    if current_q_index < len ( test_question_ids ):
        question_id_to_fetch = test_question_ids[current_q_index]
        print ( f"DEBUG: Attempting to fetch question ID: {question_id_to_fetch} from test_questions_order." )
        all_q_map = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
        question = all_q_map.get ( question_id_to_fetch )

        if not question:
            print (
                f"ERROR: Question ID {question_id_to_fetch} not found in full_question_bank! This indicates a data issue." )
            return jsonify ( {
                "message": "Question not found in question bank. Data inconsistency.",
                "type": "error"
            } ), 500
        print (
            f"DEBUG: Found question: '{question['title']}' (ID: {question['id']}) for current index {current_q_index}." )

        saved_answer = candidate_session['answers'].get ( question['id'], {} )
        code_to_display = saved_answer.get ( 'code' )
        saved_language = saved_answer.get ( 'language', 'python' )

        # Determine the initial language for the editor
        # Check if the question has a 'languages' field and if it's a list.
        # If it's a list, pick the first one or default to 'python'.
        # If it's a dictionary (new format from save_question), use initial_lang from there.
        available_languages_for_question = []
        languages_data_from_question = question.get ( 'languages' )

        if isinstance ( languages_data_from_question, list ):
            available_languages_for_question = languages_data_from_question
        elif isinstance ( languages_data_from_question, dict ):
            available_languages_for_question = list ( languages_data_from_question.keys () )

        initial_lang = saved_language if saved_language in available_languages_for_question else (
            available_languages_for_question[0] if available_languages_for_question else 'python'
        )

        starter_code = None
        if isinstance ( languages_data_from_question, dict ):
            # New format: languages is a dictionary mapping lang to details
            starter_code = languages_data_from_question.get ( initial_lang, {} ).get ( 'starter_code' )
        else:
            # Old format: languages is a list of strings. Provide a generic starter code.
            comment_symbol = '#' if initial_lang == 'python' else '//'
            starter_code = f'{comment_symbol} No specific starter code for {initial_lang}. Please write your solution here.'

        if not code_to_display or is_placeholder_code ( code_to_display, saved_language ):
            code_to_display = starter_code
        else:
            print ( f"DEBUG: Using previously saved code for question {question_id_to_fetch}." )

        # Ensure 'languages' sent to frontend is always a dictionary
        languages_to_frontend = {}
        if isinstance ( languages_data_from_question, dict ):
            languages_to_frontend = languages_data_from_question
        elif isinstance ( languages_data_from_question, list ):
            languages_to_frontend = {lang: {"starter_code": ""} for lang in available_languages_for_question}

        return jsonify ( {
            "status": "next_question",
            "current_question": {
                "id": question['id'],
                "title": question['title'],
                "description": question['description'],
                "languages": languages_to_frontend,  # Ensure this is always a dictionary
                "test_cases": question.get ( 'test_cases', [] ),  # Ensure this uses test_cases
                "starter_code": code_to_display,
                "sample_test_cases": question.get ( "test_cases", [] )  # For displaying sample test cases
            },
            "current_question_number": current_q_index + 1,
            "total_questions": len ( test_question_ids ),
            "start_time": candidate_session['start_time'].isoformat (),
            "time_limit_minutes": candidate_session['time_limit_minutes'],
            "saved_code": code_to_display
        } ), 200
    else:
        print ( f"DEBUG: All questions presented for {candidate_user_id}. Ready for final submission." )
        return jsonify ( {
            "message": "All questions have been presented. Ready for final submission.",
            "all_questions_presented": True,
            "status": "completed"
        } ), 200


@app.route ( '/api/candidates/test/submit_code/<candidate_user_id>', methods=['POST'] )
def submit_code(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header: auth_token = auth_token_header.split ( "Bearer " )[1]
    expected_token = candidate_session.get ( 'token' ) if candidate_session else None
    if not candidate_session or auth_token != expected_token:
        return jsonify ( {"message": "Unauthorized or session expired."} ), 401
    if candidate_session.get ( 'is_completed' ):
        return jsonify ( {"message": "Test already completed, cannot run code."} ), 403
    data = request.get_json ()
    question_id = data.get ( 'question_id' )
    code = data.get ( 'code' )
    language = data.get ( 'language' )
    candidate_session = test_sessions.get ( candidate_user_id )

    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header:
        auth_token = auth_token_header.split ( "Bearer " )[1]
    all_q_map = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
    current_question_obj = all_q_map.get ( question_id )
    if not current_question_obj:
        return jsonify (
            {"message": "Question not found in your current test session (or global bank). Data inconsistency."} ), 404

    results = []
    # Use test_cases for 'Run Code' button
    test_cases_to_run = current_question_obj.get ( 'test_cases', [] )
    overall_passed = True  # Assume true until a test case fails

    if is_placeholder_code ( code, language ):
        results.append ( {
            "input": "N/A",
            "expected_output": "N/A",
            "actual_output": "No valid code submitted or placeholder code detected. Please write your solution.",
            "passed": False,
            "error": "Placeholder code detected."
        } )
        overall_passed = False
    elif not test_cases_to_run:
        # If no test cases, just run once without input and report
        actual_output_str, error_output_str, execution_success = execute_code_with_subprocess ( code, language, "" )

        # Apply sanitize_output to actual_output_str before displaying/comparing
        sanitized_actual_output = sanitize_output ( actual_output_str )

        results.append ( {
            "input": "No test cases provided",
            "expected_output": "N/A",
            "actual_output": sanitized_actual_output if execution_success else f"ERROR: {error_output_str}",
            "passed": execution_success and not error_output_str,
            "error": error_output_str if not execution_success or error_output_str else None
        } )
        overall_passed = execution_success and not error_output_str
    else:
        for tc_index, tc in enumerate ( test_cases_to_run ):
            # Use tc['input'] for execution
            # IMPORTANT: Add newline to input_data for robust execution
            tc_actual_output, tc_error_output, tc_execution_success = execute_code_with_subprocess (
                code, language, tc['input']
            )

            # Apply sanitize_output to tc_actual_output before comparison
            sanitized_actual_output = sanitize_output ( tc_actual_output )

            # Fix: Access 'expected_output' directly for tests
            expected_normalized = tc.get ( 'expected_output', '' ).strip ()
            actual_normalized = sanitized_actual_output.strip ()

            test_case_passed = False
            error_message = None

            if not tc_execution_success:
                error_message = f"Execution Error (Subprocess):\n{tc_error_output}\n--- Stdout captured:\n{tc_actual_output}"
                overall_passed = False
            elif tc_error_output:
                error_message = f"Runtime Error:\n{tc_error_output}\n--- Stdout captured:\n{tc_actual_output}"
                overall_passed = False
            elif actual_normalized == expected_normalized:
                test_case_passed = True
            else:
                test_case_passed = False
                error_message = (
                    f"Output Mismatch:\n"
                    f"Expected:\n'{expected_normalized}'\n"
                    f"Actual:\n'{actual_normalized}'"
                )
                overall_passed = False  # If any test case fails, overall_passed becomes False

            results.append ( {
                "input": tc['input'],
                "expected_output": expected_normalized,
                "actual_output": actual_normalized,
                "error": error_message,
                "passed": test_case_passed,
                "test_case_number": tc_index + 1  # Add test case number for display
            } )

    overall_status_message = "✅ All Tests Passed!" if overall_passed else "❌ Some Tests Failed"

    candidate_session['answers'][question_id] = {
        'code': code,
        'language': language,
        'submission_time': datetime.now (),
        'results': results  # Store test results here
    }
    save_data ( test_sessions, TEST_SESSIONS_FILE )

    return jsonify ( {
        "output": results[0]['actual_output'] if results and not results[0]['error'] else None,
        # For single output display
        "error": results[0]['error'] if results and results[0]['error'] else None,  # For single error display
        "results": results,
        "overall_passed": overall_passed,
        "overall_message": overall_status_message
    } ), 200


@app.route ( '/api/candidates/test/save_code/<candidate_user_id>', methods=['POST'] )
def save_code_endpoint(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header:
        auth_token = auth_token_header.split ( "Bearer " )[1]
    expected_token = candidate_session.get ( 'token' ) if candidate_session else None
    if not candidate_session or auth_token != expected_token:
        return jsonify ( {"message": "Unauthorized or session expired."} ), 401
    if candidate_session.get ( 'is_completed' ):
        return jsonify ( {"message": "Test already completed, cannot save code."} ), 403
    data = request.get_json ()
    question_id = data.get ( 'question_id' )
    code = data.get ( 'code' )
    language = data.get ( 'language' )
    all_q_map = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
    question_obj = all_q_map.get ( question_id )
    if not question_obj:
        return jsonify ( {"message": "Question not found in question bank. Data inconsistency."} ), 404
    if question_id not in candidate_session['answers']:
        candidate_session['answers'][question_id] = {
            'code': '', 'language': '', 'submission_time': None, 'results': [], 'final_results': []
        }
    candidate_session['answers'][question_id]['code'] = code
    candidate_session['answers'][question_id]['language'] = language
    candidate_session['answers'][question_id]['submission_time'] = datetime.now ()
    save_data ( test_sessions, TEST_SESSIONS_FILE )
    return jsonify ( {"message": "Code saved successfully!"} ), 200


@app.route ( '/api/candidates/test/next_question/<candidate_user_id>', methods=['POST'] )
def next_question(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header:
        auth_token = auth_token_header.split ( "Bearer " )[1]
    expected_token = candidate_session.get ( 'token' ) if candidate_session else None
    if not candidate_session or auth_token != expected_token:
        return jsonify ( {"message": "Unauthorized or session expired."} ), 401
    if candidate_session.get ( 'is_completed' ):
        return jsonify ( {"message": "Test already completed, cannot advance."} ), 403
    data = request.get_json ()
    question_id = data.get ( 'question_id' )
    code = data.get ( 'code' )
    language = data.get ( 'language' )
    if question_id not in candidate_session['answers']:
        candidate_session['answers'][question_id] = {}
    candidate_session['answers'][question_id]['code'] = code
    candidate_session['answers'][question_id]['language'] = language
    candidate_session['answers'][question_id]['submission_time'] = datetime.now ()
    candidate_session['current_question_index'] += 1
    print (
        f"DEBUG: next_question called for {candidate_user_id}. New index: {candidate_session['current_question_index']}" )
    save_data ( test_sessions, TEST_SESSIONS_FILE )
    return get_current_question ( candidate_user_id )


@app.route ( '/api/candidates/test/final_submit/<candidate_user_id>', methods=['POST'] )
def final_submit(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    auth_token_header = request.headers.get ( 'Authorization' )
    auth_token = None
    if auth_token_header and "Bearer " in auth_token_header:
        auth_token = auth_token_header.split ( "Bearer " )[1]
    expected_token = candidate_session.get ( 'token' ) if candidate_session else None
    if not candidate_session or auth_token != expected_token:
        return jsonify ( {"message": "Unauthorized or session expired."} ), 401
    if candidate_session.get ( 'is_completed' ):
        return jsonify ( {"message": "Test already submitted.", "score": candidate_session.get ( 'score' )} ), 200
    try:
        total_score = 0
        test_question_ids_in_order = candidate_session.get ( 'test_questions_order', [] )
        num_questions_in_test = len ( test_question_ids_in_order )
        all_q_map = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
        for question_id_in_session_order in test_question_ids_in_order:
            # FIX: Use all_q_map instead of undefined question_lookup
            question_obj = all_q_map.get ( question_id_in_session_order )
            if not question_obj:
                if question_id_in_session_order not in candidate_session['answers']:
                    candidate_session['answers'][question_id_in_session_order] = {}
                candidate_session['answers'][question_id_in_session_order]['final_results'] = [{
                    "input": "N/A", "expected_output": "N/A", "actual_output": "Question data missing from bank.",
                    "passed": False
                }]
                candidate_session['answers'][question_id_in_session_order]['score_earned'] = 0
                continue
            answer_data = candidate_session['answers'].get ( question_obj['id'], {} )
            code_submitted = answer_data.get ( 'code', '' )
            language_submitted = answer_data.get ( 'language', 'python' )
            question_score_earned = 0
            final_results_for_q = []
            if not is_placeholder_code ( code_submitted, language_submitted ):
                full_test_cases = question_obj.get ( 'test_cases', [] )
                num_full_test_cases = len ( full_test_cases )
                if num_full_test_cases == 0:
                    question_score_earned = 100
                    final_results_for_q = [{
                        "input": "N/A", "expected_output": "N/A",
                        "actual_output": "No full test cases defined for grading. Assigned full score.", "passed": True
                    }]
                else:
                    passed_test_cases_count = 0
                    for tc_idx, tc in enumerate ( full_test_cases ):
                        # IMPORTANT: Add newline to input_data for robust execution
                        tc_actual_output, tc_error_output, tc_execution_success = execute_code_with_subprocess (
                            code_submitted, language_submitted, tc['input'], timeout=10 )

                        # Apply sanitize_output to tc_actual_output before comparison
                        sanitized_actual_output = sanitize_output ( tc_actual_output )

                        # FIX: Changed to directly access 'expected_output'
                        expected_output_for_grading = tc.get ( 'expected_output', '' ).strip ()
                        actual_normalized = sanitized_actual_output.strip ()  # Use sanitized output for comparison
                        test_case_passed = False
                        current_result_output = ""
                        if not tc_execution_success:
                            current_result_output = f"Execution Error (Subprocess):\n{tc_error_output}\n--- Stdout captured:\n{tc_actual_output}"
                        elif tc_error_output:
                            current_result_output = f"Runtime Error:\n{tc_error_output}\n--- Stdout captured:\n{tc_actual_output}"
                        elif actual_normalized == expected_output_for_grading:
                            test_case_passed = True
                            current_result_output = tc_actual_output
                        else:
                            test_case_passed = False
                            current_result_output = (
                                f"Output Mismatch:\n"
                                f"Expected:\n'{expected_output_for_grading}'\n"
                                f"Actual:\n'{actual_normalized}'\n"
                                f"--- Raw Output ---\n{tc_actual_output}"
                            )
                        if test_case_passed:
                            passed_test_cases_count += 1
                        final_results_for_q.append ( {
                            "input": tc['input'],
                            "expected_output": expected_output_for_grading,
                            "actual_output": current_result_output,
                            "passed": test_case_passed
                        } )
                    if num_full_test_cases > 0:
                        question_score_earned = (passed_test_cases_count / num_full_test_cases) * 100
                    else:
                        question_score_earned = 0
                candidate_session['answers'][question_obj['id']]['final_results'] = final_results_for_q
                candidate_session['answers'][question_obj['id']]['score_earned'] = question_score_earned
                total_score += question_score_earned
            else:
                if question_obj['id'] not in candidate_session['answers']:
                    candidate_session['answers'][question_obj['id']] = {}
                candidate_session['answers'][question_obj['id']]['final_results'] = [{
                    "input": "N/A",
                    "expected_output": "N/A",
                    "actual_output": "No code submitted or placeholder code detected.",
                    "passed": False
                }]
                candidate_session['answers'][question_obj['id']]['score_earned'] = 0
        candidate_session['score'] = round ( (total_score / num_questions_in_test),
                                             2 ) if num_questions_in_test > 0 else 0
        candidate_session['is_completed'] = True
        candidate_session['end_time'] = datetime.now ()
        save_data ( test_sessions, TEST_SESSIONS_FILE )
        return jsonify ( {
            "message": "Test submitted successfully.",
            "score": candidate_session['score'],
            "candidate_user_id": candidate_user_id,
            "redirect_url": url_for ( 'submitted_page',
                                      name=candidate_session.get ( 'full_name', 'Candidate' ),
                                      email=candidate_session.get ( 'email', 'N/A' ),
                                      answered_count=len ( candidate_session.get ( 'answers', {} ) ) )
            # Redirect to submitted.html
        } ), 200
    except Exception as e:
        import traceback
        traceback.print_exc ()
        return jsonify (
            {"message": f"An internal server error occurred during submission: {str ( e )}", "type": "error"} ), 500


@app.route ( '/api/mark_test_failed/<candidate_user_id>', methods=['POST'] )
def mark_test_failed(candidate_user_id):
    """API to mark a test session as failed (e.g., due to tab close/refresh)."""
    candidate_session = test_sessions.get ( candidate_user_id )
    if candidate_session and not candidate_session.get ( 'is_completed' ):
        candidate_session['is_completed'] = True
        candidate_session['end_time'] = datetime.now ()
        candidate_session['score'] = 0  # Assign 0 score for incomplete/failed tests
        candidate_session['status'] = 'Expired or Force Closed'  # Custom status for this scenario
        save_data ( test_sessions, TEST_SESSIONS_FILE )
        print ( f"Test for {candidate_user_id} marked as failed due to unexpected closure." )
        return jsonify ( {"message": "Test marked as failed."} ), 200
    return jsonify ( {"message": "Test already completed or not found."} ), 200


@app.route ( '/force_submit/<candidate_user_id>' )
def force_submit(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    if not candidate_session:
        return redirect ( url_for ( 'link_expired' ) )

    # Mark test completed
    candidate_session['is_completed'] = True
    candidate_session['end_time'] = datetime.now ()
    save_data ( test_sessions, TEST_SESSIONS_FILE )

    # Get count of answered questions
    name = candidate_session.get ( 'full_name', 'Candidate' )
    email = candidate_session.get ( 'email', 'N/A' )
    answered_count = len ( candidate_session.get ( 'answers', {} ) )

    # Redirect to submitted page with query params
    return redirect ( url_for ( 'submitted_page', name=name, email=email, answered_count=answered_count ) )


@app.route ( '/submitted' )
def submitted_page():
    name = request.args.get ( 'name', 'Candidate' )
    email = request.args.get ( 'email', 'N/A' )
    answered_count = request.args.get ( 'answered_count', 'N/A' )

    return render_template ( 'submitted.html', name=name, email=email, answered_count=answered_count )


@app.route ( '/submission_success/<candidate_user_id>' )
def submission_success(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    if not candidate_session:
        return redirect ( url_for ( 'login', error="Invalid submission ID or test not found." ) )
    if 'logged_in' not in session or not session['logged_in'] or session.get ( 'user_role' ) not in ['admin',
                                                                                                     'reviewer']:
        if not candidate_session.get ( 'is_completed' ):
            return redirect ( url_for ( 'link_expired', message="You are not authorized to view this report." ) )
    score = candidate_session.get ( 'score', 'N/A' )
    question_details_for_display = []
    test_question_ids_order = candidate_session.get ( 'test_questions_order', [] )
    answers = candidate_session.get ( 'answers', {} )
    question_lookup = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
    for idx, q_id in enumerate ( test_question_ids_order ):
        full_q_details = question_lookup.get ( q_id, {
            'id': q_id,
            'title': f'Question Not Found (ID: {q_id})',
            'description': 'Data missing from the question bank.',
            'languages': {'python': {'starter_code': '// Question not found'}},
            'test_cases': [],  # Removed duplicate 'test_cases' key
        } )
        submitted_answer = answers.get ( q_id, {} )
        # Handle languages being a list or a dictionary
        languages_data_from_q_details = full_q_details.get ( 'languages' )
        language = submitted_answer.get ( 'language' )
        if not language:  # If no language saved, try to infer from question details
            # Check if languages_data_from_q_details is not None and is a dictionary
            if isinstance ( languages_data_from_q_details, dict ) and languages_data_from_q_details:
                language = next ( iter ( languages_data_from_q_details.keys () ), 'N/A' )
            elif isinstance ( languages_data_from_q_details, list ) and languages_data_from_q_details:
                language = languages_data_from_q_details[0]
            else:
                language = 'N/A'  # Default if languages field is missing or malformed

        results_to_show = submitted_answer.get ( 'final_results', submitted_answer.get ( 'results', [] ) )
        question_status = "Not Attempted"
        if 'code' in submitted_answer and not is_placeholder_code ( submitted_answer['code'], language ):
            if results_to_show and all ( r.get ( 'passed', False ) for r in results_to_show ):
                question_status = "Passed All Tests"
            elif results_to_show and any ( r.get ( 'passed', False ) for r in results_to_show ):
                question_status = "Partial Pass"
            else:
                question_status = "Failed All Tests"
        elif 'code' in submitted_answer and is_placeholder_code ( submitted_answer['code'], language ):
            question_status = "Placeholder Code Submitted"
        question_details_for_display.append ( {
            'index': idx + 1,
            'id': q_id,
            'title': full_q_details.get ( 'title', 'N/A' ),
            'description': full_q_details.get ( 'description', 'N/A' ),
            'submitted_code': submitted_answer.get ( 'code', 'No code submitted.' ),
            'language': language,
            'test_case_results': results_to_show,
            'status': question_status,
            'score_earned': submitted_answer.get ( 'score_earned', 0 )
        } )
    return render_template ( 'submission_success.html',
                             score=score,
                             candidate_user_id=candidate_user_id,
                             question_data_list=question_details_for_display,  # Changed variable name for clarity
                             candidate_info={
                                 'full_name': candidate_session.get ( 'full_name', 'N/A' ),
                                 'email': candidate_session.get ( 'email', 'N/A' ),
                                 'phone_number': candidate_session.get ( 'phone_number', 'N/A' ),
                                 'test_started_at': candidate_session['start_time'].strftime (
                                     '%Y-%m-%d %H:%M:%S' ) if isinstance ( candidate_session.get ( 'start_time' ),
                                                                           datetime ) else 'N/A',
                                 'test_completed': candidate_session.get ( 'is_completed', False )
                             } )


@app.route ( '/logout' )
def logout():
    session.pop ( 'logged_in', None )
    session.pop ( 'user_email', None )
    session.pop ( 'user_role', None )
    session.pop ( 'user_fullname', None )
    return redirect ( url_for ( 'login', success="You have been logged out." ) )


@app.route ( '/admin/questions/save', methods=['POST'] )
def save_question():
    global full_question_bank
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401
    data = request.get_json ()
    original_question_id = data.get ( 'question_id' )  # This is the ID entered by the admin
    title = data.get ( 'title' )
    description = data.get ( 'description' )
    language = data.get ( 'language' )
    starter_code = data.get ( 'starter_code' )
    sample_test_cases = data.get ( 'sample_test_cases' )
    full_test_cases = data.get ( 'full_test_cases' )
    category = data.get ( 'category', 'misc' ).strip ()

    if not all ( [title, description, language, starter_code, sample_test_cases, full_test_cases, category] ):
        return jsonify ( {"message": "Missing required fields.", "type": "error"} ), 400

    # Validate test cases JSON structure
    if not isinstance ( sample_test_cases, list ) or not all (
            isinstance ( tc, dict ) and 'input' in tc and 'expected_output' in tc for tc in sample_test_cases ):
        return jsonify ( {"message": "Sample test cases must be a list of objects with 'input' and 'expected_output'.",
                          "type": "error"} ), 400
    if not isinstance ( full_test_cases, list ) or not all (
            isinstance ( tc, dict ) and 'input' in tc and 'expected_output' in tc for tc in full_test_cases ):
        return jsonify ( {"message": "Full test cases must be a list of objects with 'input' and 'expected_output'.",
                          "type": "error"} ), 400

    # Determine the unique ID for the question
    # If an original_question_id is provided, it means we are either updating an existing one
    # or creating a new one with a specific ID.
    # The unique_id will always be category_original_id.
    if not original_question_id:
        # If no ID provided, generate a random one (for new questions)
        original_question_id = str ( uuid.uuid4 () )[:8].upper ()

    unique_question_id = f"{category}_{original_question_id}"

    new_question_data = {
        "id": unique_question_id,  # Store the unique ID
        "title": title,
        "description": description,
        "languages": {
            language: {
                "starter_code": starter_code
            }
        },
        "sample_test_cases": sample_test_cases,
        "test_cases": full_test_cases,
        "_original_id": original_question_id,  # Store original ID for admin display
        "_category_key": category  # Store category key for admin display
    }
    category_filepath = os.path.join ( QUESTION_BANK_DIR, f"{category}.json" )
    category_questions = []
    if os.path.exists ( category_filepath ):
        try:
            with open ( category_filepath, 'r', encoding='utf-8' ) as f:
                category_questions = json.load ( f )
        except json.JSONDecodeError:
            print ( f"WARNING: JSONDecodeError in {category_filepath}. Initializing as empty list." )
            category_questions = []  # If file is corrupted, treat as empty

    question_exists = False
    for i, q in enumerate ( category_questions ):
        if q['id'] == unique_question_id:  # Compare with the unique ID
            # Update existing question
            # Merge languages: keep existing languages, update/add the current one
            if 'languages' not in q or not isinstance ( q['languages'], dict ):
                q['languages'] = {}  # Initialize if missing or not dict
            q['languages'][language] = {"starter_code": starter_code}

            q.update ( {
                "title": title,
                "description": description,
                "sample_test_cases": sample_test_cases,
                "test_cases": full_test_cases,
                "_original_id": original_question_id,  # Ensure original ID is preserved/updated
                "_category_key": category
            } )
            category_questions[i] = q
            question_exists = True
            message_text = f"Question '{original_question_id}' updated successfully in category '{category}' (Unique ID: {unique_question_id})."
            break
    if not question_exists:
        category_questions.append ( new_question_data )
        message_text = f"Question '{original_question_id}' added successfully to category '{category}' (Unique ID: {unique_question_id})."

    with open ( category_filepath, 'w', encoding='utf-8' ) as f:
        json.dump ( category_questions, f, indent=4 )

    # Reload the global question bank to reflect changes
    full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )

    return jsonify ( {"message": message_text, "type": "success", "question_id": unique_question_id} ), 200


@app.route ( '/admin/questions/get/<question_id>', methods=['GET'] )
def get_question_details(question_id):
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access."} ), 401
    question = None
    category_key_from_param = request.args.get ( 'category' )  # Optional, for filtering search

    # Iterate through the full_question_bank (which has unique IDs)
    for cat_key, questions_list in full_question_bank.items ():
        for q in questions_list:
            if q['id'] == question_id:  # Match against the unique ID
                question = q
                category_key_from_param = cat_key  # Ensure we know the actual category
                break
        if question:
            break

    if question:
        question_data = question.copy ()
        # Ensure test_cases are correctly represented for JSON dump
        question_data['test_cases_json'] = json.dumps ( question_data.get ( 'test_cases', [] ), indent=2 )
        question_data['sample_test_cases_json'] = json.dumps ( question_data.get ( 'sample_test_cases', [] ), indent=2 )

        # Determine the language and starter code to pre-fill the form
        first_lang = 'python'
        starter_code = ''
        languages_data = question_data.get ( 'languages' )

        if isinstance ( languages_data, dict ) and languages_data:
            # Try to get the language used for the last save if available, or just the first one
            # The dashboard form only allows selecting one language at a time for editing
            # So, we'll pick the first language found in the 'languages' dict to populate the form
            first_lang = next ( iter ( languages_data.keys () ) )
            starter_code = languages_data.get ( first_lang, {} ).get ( 'starter_code', '' )
        elif isinstance ( languages_data, list ) and languages_data:  # Handle old list format for compatibility
            first_lang = languages_data[0]
            starter_code = f'// No specific starter code for {first_lang}.'  # Default for old format

        # Provide the original ID and category for the admin form
        question_data['question_id'] = question_data.get ( '_original_id', question_id.split ( '_', 1 )[
            -1] if '_' in question_id else question_id )
        question_data['category'] = question_data.get ( '_category_key', category_key_from_param )
        question_data['language'] = first_lang
        question_data['starter_code'] = starter_code

        return jsonify ( question_data ), 200
    else:
        return jsonify ( {"message": "Question not found."} ), 404


@app.route ( '/admin/questions/delete/<question_id>', methods=['POST'] )
def delete_question(question_id):
    global full_question_bank
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401
    message_text = "Question not found."
    message_type = "error"
    deleted = False
    requested_category = request.args.get ( 'category' )
    categories_to_check = [requested_category] if requested_category else list ( full_question_bank.keys () )
    for category_key in categories_to_check:
        if category_key not in full_question_bank:
            continue
        category_filepath = os.path.join ( QUESTION_BANK_DIR, f"{category_key}.json" )
        if os.path.exists ( category_filepath ):
            with open ( category_filepath, 'r', encoding='utf-8' ) as f:
                try:
                    questions_in_category = json.load ( f )
                except json.JSONDecodeError:
                    questions_in_category = []
            original_len = len ( questions_in_category )
            questions_in_category = [q for q in questions_in_category if q['id'] != question_id]
            if len ( questions_in_category ) < original_len:
                if questions_in_category:
                    with open ( category_filepath, 'w', encoding='utf-8' ) as f:
                        json.dump ( questions_in_category, f, indent=4 )
                else:
                    os.remove ( category_filepath )
                message_text = f"Question '{question_id}' deleted successfully from category '{category_key}'."
                message_type = "success"
                deleted = True
                break
    if deleted:
        full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )
        return jsonify ( {"message": message_text, "type": message_type} ), 200
    else:
        return jsonify ( {"message": message_text, "type": message_type} ), 404


@app.route ( '/admin/questions/all_questions', methods=['GET'] )
def get_all_questions_api():
    print ( "[DEBUG] /admin/questions/all_questions route hit." )
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        print ( "[DEBUG] Unauthorized access to /admin/questions/all_questions. Session or role mismatch." )
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401

    global full_question_bank
    try:
        # Reload the question bank to ensure it's up-to-date with any file changes
        full_question_bank = loader.load_question_bank ( QUESTION_BANK_DIR )
        print ( f"[DEBUG] Successfully reloaded question bank. Found {len ( full_question_bank )} categories." )
        print ( f"[DEBUG] Categories: {list ( full_question_bank.keys () )}" )

        # Check if any questions were actually loaded
        total_questions_loaded = sum ( len ( q_list ) for q_list in full_question_bank.values () )
        if total_questions_loaded == 0:
            print ( "[WARNING] Question bank is empty after loading. No questions to display." )

        return jsonify ( full_question_bank ), 200
    except Exception as e:
        print ( f"[ERROR] Exception in /admin/questions/all_questions: {e}" )
        import traceback
        traceback.print_exc ()  # Print full traceback to server console
        return jsonify ( {"message": f"Failed to load question bank: {str ( e )}", "type": "error"} ), 500


@app.route ( '/admin/users/save', methods=['POST'] )
def save_user():
    global users
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401
    email = request.form['email'].lower ()
    fullname = request.form['fullname']
    password = request.form['password']
    role = request.form['role']
    if not all ( [email, fullname, role] ):
        return jsonify ( {"message": "Email, Full Name, and Role are required.", "type": "error"} ), 400
    if not is_allowed_email_domain ( email ):
        return jsonify (
            {"message": f"Only {', '.join ( OUTLOOK_DOMAINS )} email addresses are allowed.", "type": "error"} ), 400
    message_text = ""
    is_new_user = email not in users
    if is_new_user:
        if not password:
            return jsonify ( {"message": "Password is required for new users.", "type": "error"} ), 400
        if len ( password ) < 8:
            return jsonify ( {"message": "Password must be at least 8 characters long.", "type": "error"} ), 400
        users[email] = {
            'fullname': fullname,
            'password_hash': hashlib.sha256 ( password.encode () ).hexdigest (),  # Hash the new password
            'role': role
        }
        message_text = f"User '{email}' added successfully."
    else:
        users[email]['fullname'] = fullname
        users[email]['role'] = role
        if password and password != "********":  # "********" is typically a placeholder for untouched password fields
            if len ( password ) < 8:
                return jsonify ( {"message": "New password must be at least 8 characters long.", "type": "error"} ), 400
            users[email]['password_hash'] = hashlib.sha256 ( password.encode () ).hexdigest ()  # Hash the new password
        message_text = f"User '{email}' updated successfully."
    save_data ( users, USERS_FILE )
    return jsonify ( {"message": message_text, "type": "success"} ), 200


@app.route ( '/admin/users/delete/<user_email>', methods=['POST'] )
def delete_user(user_email):
    global users
    if 'logged_in' not in session or session.get ( 'user_role' ) != 'admin':
        return jsonify ( {"message": "Unauthorized access. Admins only.", "type": "error"} ), 401
    if user_email == ADMIN_SENDER_EMAIL:
        return jsonify ( {"message": "Cannot delete the main admin account.", "type": "error"} ), 403
    if user_email in users:
        del users[user_email]
        save_data ( users, USERS_FILE )
        return jsonify ( {"message": f"User '{user_email}' deleted successfully.", "type": "success"} ), 200
    else:
        return jsonify ( {"message": "User not found.", "type": "error"} ), 404


@app.route ( '/forgot_password_request', methods=['POST'] )
def forgot_password_request():
    email = request.json.get ( 'email', '' ).lower ()
    if not email:
        return jsonify ( {"message": "Email is required.", "type": "error"} ), 400
    user_info = users.get ( email )
    if not user_info:
        return jsonify (
            {"message": "If the email is registered, a password reset link has been sent.", "type": "success"} ), 200
    token = str ( uuid.uuid4 () )
    expires_at = datetime.now () + timedelta ( minutes=15 )
    password_reset_tokens[token] = {
        'email': email,
        'expires_at': expires_at
    }
    save_data ( password_reset_tokens, PASSWORD_RESET_TOKENS_FILE )
    reset_link = url_for ( 'dashboard', view='profile', action='reset_password', token=token, _external=True )
    subject = "Password Reset Request for Your Account"
    body = f"""
Dear {user_info.get ( 'fullname', 'User' )},

You have requested a password reset for your account.
Please click on the following link to reset your password:

{reset_link}

This link will expire in 15 minutes. If you did not request a password reset, please ignore this email.

Regards,
The Testing Team
"""
    if send_outlook_email ( email, subject, body ):
        return jsonify (
            {"message": "If the email is registered, a password reset link has been sent.", "type": "success"} ), 200
    else:
        password_reset_tokens.pop ( token, None )
        save_data ( password_reset_tokens, PASSWORD_RESET_TOKENS_FILE )
        return jsonify ( {"message": "Failed to send reset email. Please try again later.", "type": "error"} ), 500


@app.route ( '/reset_password_perform', methods=['POST'] )
def reset_password_perform():
    token = request.json.get ( 'token' )
    new_password = request.json.get ( 'new_password' )
    confirm_new_password = request.json.get ( 'confirm_new_password' )
    if not token or not new_password or not confirm_new_password:
        return jsonify ( {"message": "All fields are required.", "type": "error"} ), 400
    if new_password != confirm_new_password:
        return jsonify ( {"message": "Passwords do not match.", "type": "error"} ), 400
    if len ( new_password ) < 8:
        return jsonify ( {"message": "New password must be at least 8 characters long.", "type": "error"} ), 400
    reset_token_data = password_reset_tokens.get ( token )
    if not reset_token_data:
        return jsonify ( {"message": "Invalid or expired password reset link.", "type": "error"} ), 400
    if datetime.now () > reset_token_data['expires_at']:
        password_reset_tokens.pop ( token, None )
        save_data ( password_reset_tokens, PASSWORD_RESET_TOKENS_FILE )
        return jsonify (
            {"message": "Password reset link has expired. Please request a new one.", "type": "error"} ), 400
    user_email_to_reset = reset_token_data['email']
    if user_email_to_reset not in users:
        return jsonify ( {"message": "User associated with this link not found.", "type": "error"} ), 404
    users[user_email_to_reset]['password_hash'] = hash_password ( new_password )
    save_data ( users, USERS_FILE )
    password_reset_tokens.pop ( token, None )
    save_data ( password_reset_tokens, PASSWORD_RESET_TOKENS_FILE )
    return jsonify (
        {"message": "Your password has been successfully reset. You can now log in with your new password.",
         "type": "success"} ), 200


def _break_long_text_into_chunks(text, max_line_width=90):
    if not text:
        return ""
    lines = text.splitlines ()
    broken_lines = []
    for line in lines:
        if len ( line ) > max_line_width:
            words = line.split ( ' ' )
            current_line_parts = []
            current_line_len = 0
            for word in words:
                if current_line_len + len ( word ) + 1 > max_line_width and current_line_len > 0:
                    broken_lines.append ( ' '.join ( current_line_parts ) )
                    current_line_parts = [word]
                    current_line_len = len ( word )
                else:
                    current_line_parts.append ( word )
                    current_line_len += len ( word ) + 1
            if current_line_parts:
                broken_lines.append ( ' '.join ( current_line_parts ) )
        else:
            broken_lines.append ( line )
    final_broken_lines = []
    for line in broken_lines:
        for i in range ( 0, len ( line ), max_line_width ):
            final_broken_lines.append ( line[i:i + max_line_width] )
    return "\n".join ( final_broken_lines )


@app.route ( '/api/test_report_pdf/<candidate_user_id>', methods=['GET'] )
def generate_test_report_pdf(candidate_user_id):
    if 'logged_in' not in session or not session['logged_in']:
        return jsonify ( {"message": "Unauthorized access."} ), 401
    candidate_session = test_sessions.get ( candidate_user_id )
    if not candidate_session:
        return jsonify ( {"message": "Candidate test session not found."} ), 404
    try:
        pdf = FPDF ()
        pdf.add_page ()
        pdf.set_auto_page_break ( auto=True, margin=15 )
        pdf.set_font ( "Helvetica", "B", 24 )
        pdf.cell ( 0, 10, "L1 Qualification Test Report", 0, 1, "C" )
        pdf.ln ( 10 )
        pdf.set_font ( "Helvetica", "B", 12 )
        pdf.cell ( 0, 8, "Candidate Information:", 0, 1, "L" )
        pdf.set_font ( "Helvetica", "", 10 )
        pdf.cell ( 0, 6, f"Full Name: {candidate_session.get ( 'full_name', 'N/A' )}", 0, 1 )
        pdf.cell ( 0, 6, f"Email: {candidate_session.get ( 'email', 'N/A' )}", 0, 1 )
        pdf.cell ( 0, 6, f"Phone Number: {candidate_session.get ( 'phone_number', 'N/A' )}", 0, 1 )
        start_time_str = candidate_session.get ( 'start_time', 'N/A' )
        if isinstance ( start_time_str, datetime ):
            start_time_str = start_time_str.strftime ( '%Y-%m-%d %H:%M:%S' )
        pdf.cell ( 0, 6, f"Test Started: {start_time_str}", 0, 1 )
        pdf.cell ( 0, 6, f"Test Completed: {'Yes' if candidate_session.get ( 'is_completed', False ) else 'No'}", 0, 1 )
        pdf.cell ( 0, 6, f"Final Score: {candidate_session.get ( 'score', 'N/A' )}%", 0, 1 )
        pdf.ln ( 10 )
        pdf.set_font ( "Helvetica", "B", 14 )
        pdf.cell ( 0, 10, "Test Details:", 0, 1, "L" )
        pdf.ln ( 2 )
        test_question_ids = candidate_session.get ( 'test_questions_order', [] )
        answers = candidate_session.get ( 'answers', {} )
        question_lookup = {q['id']: q for q in get_all_questions_flattened ( full_question_bank )}
        if not test_question_ids:
            pdf.set_font ( "Helvetica", "", 10 )
            pdf.multi_cell ( 0, 6, "No questions found for this test session." )
            pdf.ln ( 5 )
        for idx, q_id in enumerate ( test_question_ids ):
            question_obj = question_lookup.get ( q_id )
            if not question_obj:
                continue
            submitted_answer = answers.get ( q_id, {} )
            # Handle languages being a list or a dictionary
            languages_data_from_q_details = question_obj.get ( 'languages' )  # Corrected from full_q_details
            language = submitted_answer.get ( 'language' )
            if not language:  # If no language saved, try to infer from question details
                # Check if languages_data_from_q_details is not None and is a dictionary
                if isinstance ( languages_data_from_q_details, dict ) and languages_data_from_q_details:
                    language = next ( iter ( languages_data_from_q_details.keys () ), 'N/A' )
                elif isinstance ( languages_data_from_q_details, list ) and languages_data_from_q_details:
                    language = languages_data_from_q_details[0]
                else:
                    language = 'N/A'  # Default if languages field is missing or malformed

            results_to_show = submitted_answer.get ( 'final_results', submitted_answer.get ( 'results', [] ) )
            question_status = "Not Attempted"
            if 'code' in submitted_answer and not is_placeholder_code ( submitted_answer['code'], language ):
                if results_to_show and all ( r.get ( 'passed', False ) for r in results_to_show ):
                    question_status = "Passed All Tests"
                elif results_to_show and any ( r.get ( 'passed', False ) for r in results_to_show ):
                    question_status = "Partial Pass"
                else:
                    question_status = "Failed All Tests"
            elif 'code' in submitted_answer and is_placeholder_code ( submitted_answer['code'], language ):
                question_status = "Placeholder Code Submitted"
            pdf.set_font ( "Helvetica", "BU", 12 )
            pdf.cell ( 0, 8, f"Question {idx + 1}: {question_obj.get ( 'title', 'N/A' )} (ID: {q_id})", 0, 1, "L" )
            pdf.set_font ( "Helvetica", "", 10 )
            pdf.multi_cell ( 0, 6, f"Description: {question_obj.get ( 'description', 'N/A' )}" )
            pdf.ln ( 2 )
            pdf.set_font ( "Helvetica", "B", 10 )
            pdf.cell ( 0, 6, f"Submitted Code ({submitted_answer.get ( 'language', 'N/A' )}):", 0, 1 )
            pdf.set_font ( "Courier", "", 9 )
            code_content = submitted_answer.get ( 'code', 'No code submitted.' )
            broken_code_content = _break_long_text_into_chunks ( code_content, max_line_width=80 )
            for line in (broken_code_content or '').splitlines ():
                pdf.multi_cell ( 0, 4, line )
            pdf.ln ( 4 )
            pdf.set_font ( "Helvetica", "B", 10 )
            pdf.cell ( 0, 6, "Test Case Results:", 0, 1 )
            pdf.set_font ( "Helvetica", "", 9 )
            results = submitted_answer.get ( 'final_results', [] )
            if not results:
                pdf.cell ( 0, 6, "No detailed test case results available.", 0, 1 )
            else:
                for tc_idx, result in enumerate ( results ):
                    status = "PASSED" if result.get ( 'passed' ) else "FAILED"
                    pdf.set_fill_color ( 190, 255, 190 ) if result.get ( 'passed' ) else pdf.set_fill_color ( 255, 190,
                                                                                                              190 )
                    pdf.cell ( 0, 6, f"  Test Case {tc_idx + 1}: {status}", 0, 1, "L", 1 )
                    pdf.set_fill_color ( 255, 255, 255 )
                    pdf.multi_cell ( 0, 5,
                                     f"    Input: {_break_long_text_into_chunks ( result.get ( 'input', 'N/A' ), max_line_width=100 )}" )
                    pdf.multi_cell ( 0, 5,
                                     f"    Expected Output: {_break_long_text_into_chunks ( result.get ( 'expected_output', 'N/A' ), max_line_width=100 )}" )
                    pdf.multi_cell ( 0, 5,
                                     f"    Actual Output: {_break_long_text_into_chunks ( result.get ( 'actual_output', 'N/A' ), max_line_width=100 )}" )
                    pdf.ln ( 2 )
            pdf.ln ( 5 )
        pdf_output = pdf.output ( dest='S' ).encode ( 'latin-1' )
        return send_file ( io.BytesIO ( pdf_output ), mimetype='application/pdf', as_attachment=True,
                           download_name=f"Test_Report_{candidate_session.get ( 'full_name', 'candidate' ).replace ( ' ', '_' )}_{datetime.now ().strftime ( '%Y%m%d%H%M%S' )}.pdf" )
    except Exception as e:
        import traceback
        traceback.print_exc ()
        return jsonify ( {"message": f"Failed to generate PDF report due to an internal server error: {e}"} ), 500


@app.route ( '/test_timeout_redirect/<candidate_user_id>' )
def test_timeout_redirect(candidate_user_id):
    candidate_session = test_sessions.get ( candidate_user_id )
    if not candidate_session:
        return redirect ( url_for ( 'link_expired' ) )

    full_name = candidate_session.get ( 'full_name', 'Candidate' )
    email = candidate_session.get ( 'email', 'N/A' )
    answered_count = len ( candidate_session.get ( 'answers', {} ) )

    # Mark test as completed (if not already)
    if not candidate_session.get ( 'is_completed' ):
        candidate_session['is_completed'] = True
        candidate_session['end_time'] = datetime.now ()
        save_data ( test_sessions, TEST_SESSIONS_FILE )

    return redirect ( url_for ( 'submitted_page', name=full_name, email=email, answered_count=answered_count ) )


if __name__ == '__main__':
    for _file in [USERS_FILE, SECURE_LINKS_FILE, TEST_SESSIONS_FILE, PASSWORD_RESET_TOKENS_FILE]:
        if not os.path.exists ( _file ):
            with open ( _file, 'w' ) as f:
                f.write ( '{}' )
        else:
            print ( f"DEBUG: File already exists: {_file}" )
    if ADMIN_SENDER_EMAIL not in users or not users[ADMIN_SENDER_EMAIL].get ( 'role' ) == 'admin':
        users[ADMIN_SENDER_EMAIL] = {
            'fullname': 'Admin',
            'password_hash': hash_password ( 'password123' ),
            'role': 'admin'
        }
        save_data ( users, USERS_FILE )
        users = load_data ( USERS_FILE )  # Reload to ensure current global 'users' is updated
    else:
        print ( f"DEBUG: Admin user '{ADMIN_SENDER_EMAIL}' already exists." )

    # Check if question bank directory exists and contains JSON files
    if not os.path.exists ( QUESTION_BANK_DIR ) or not any (
            fname.endswith ( '.json' ) for fname in os.listdir ( QUESTION_BANK_DIR ) ):
        print (
            f"WARNING: Question bank directory '{QUESTION_BANK_DIR}' is empty or does not exist. Please ensure JSON question files are placed here." )
    else:
        print ( f"DEBUG: Question bank directory '{QUESTION_BANK_DIR}' exists and contains JSON files." )

    # Check for SSL certificates for HTTPS
    ssl_cert = 'cert.pem'
    ssl_key = 'key.pem'
    if os.path.exists ( ssl_cert ) and os.path.exists ( ssl_key ):
        print ( "✅ SSL certificates found. Running in HTTPS mode." )
        ssl_context = (ssl_cert, ssl_key)
    else:
        print ( "⚠️ SSL certificates not found. Running in HTTP mode." )
        ssl_context = None

    # Run the Flask application
    app.run ( debug=True, host="0.0.0.0", port=5000, ssl_context=ssl_context )

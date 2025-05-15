import os
import xml.etree.ElementTree as ET
import requests
import qrcode
import subprocess
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from werkzeug.utils import secure_filename
import re
import shlex
import tempfile
from urllib.parse import unquote, urlparse, parse_qs

app = Flask(__name__)

# Vercel environment: Use /tmp for writable storage
VERCEL_TMP_DIR = "/tmp"
app.config['UPLOAD_FOLDER'] = os.path.join(VERCEL_TMP_DIR, 'vast_converter_uploads')
app.config['GENERATED_FOLDER'] = os.path.join(VERCEL_TMP_DIR, 'vast_converter_generated')
app.config['ALLOWED_EXTENSIONS'] = {'xml', 'txt'}

# Ensure generated and uploads folders exist in /tmp
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['GENERATED_FOLDER'], exist_ok=True)

# Definition for APP_DIR, assuming it's defined globally or near the top of the script
# For Vercel, __file__ gives the path to app.py in the /var/task directory
APP_DIR = os.path.dirname(os.path.abspath(__file__))

def get_ffmpeg_path():
    """
    Determines the appropriate path for the ffmpeg executable.
    Priority:
    1. Bundled ffmpeg (for Vercel or consistent local use if present in APP_DIR).
    2. Local macOS Homebrew ffmpeg (if available and not on Vercel).
    3. Fallback to 'ffmpeg' (hoping it's in the system PATH).
    """
    is_on_vercel = 'VERCEL' in os.environ or 'NOW_REGION' in os.environ
    ffmpeg_executable_path = ''

    print(f"[DEBUG Pathing] get_ffmpeg_path invoked. Vercel env: {is_on_vercel}, APP_DIR: {APP_DIR}", flush=True)

    # Priority 1: Bundled ffmpeg (in APP_DIR, e.g., /var/task/ffmpeg or local project root/ffmpeg)
    bundled_ffmpeg_path = os.path.join(APP_DIR, 'ffmpeg')
    print(f"[DEBUG Pathing] Checking for bundled ffmpeg at: {bundled_ffmpeg_path}", flush=True)
    if os.path.exists(bundled_ffmpeg_path):
        print(f"[DEBUG Pathing] Bundled ffmpeg FOUND at: {bundled_ffmpeg_path}", flush=True)
        if not os.access(bundled_ffmpeg_path, os.X_OK):
            print(f"[DEBUG Pathing] Bundled ffmpeg {bundled_ffmpeg_path} is NOT EXECUTABLE. Attempting chmod.", flush=True)
            try:
                os.chmod(bundled_ffmpeg_path, 0o755)
                if os.access(bundled_ffmpeg_path, os.X_OK):
                    print(f"[DEBUG Pathing] Bundled ffmpeg {bundled_ffmpeg_path} is NOW EXECUTABLE after chmod.", flush=True)
                    ffmpeg_executable_path = bundled_ffmpeg_path
                else:
                    print(f"[DEBUG Pathing] ERROR: chmod failed. Bundled ffmpeg {bundled_ffmpeg_path} still NOT executable.", flush=True)
            except Exception as e_chmod:
                print(f"[DEBUG Pathing] ERROR: Could not chmod {bundled_ffmpeg_path}: {e_chmod}", flush=True)
        else:
            print(f"[DEBUG Pathing] Using bundled ffmpeg (already executable): {bundled_ffmpeg_path}", flush=True)
            ffmpeg_executable_path = bundled_ffmpeg_path
    else:
        print(f"[DEBUG Pathing] Bundled ffmpeg NOT FOUND at: {bundled_ffmpeg_path}", flush=True)

    # Priority 2: Local macOS Homebrew ffmpeg (only if not on Vercel and bundled not found/used)
    if not ffmpeg_executable_path and not is_on_vercel:
        local_mac_ffmpeg_path = '/opt/homebrew/bin/ffmpeg'
        print(f"[DEBUG Pathing] Bundled ffmpeg not used. Checking for local macOS Homebrew ffmpeg at: {local_mac_ffmpeg_path}", flush=True)
        if os.path.exists(local_mac_ffmpeg_path) and os.access(local_mac_ffmpeg_path, os.X_OK):
            print(f"[DEBUG Pathing] Using local macOS ffmpeg (Homebrew): {local_mac_ffmpeg_path}", flush=True)
            ffmpeg_executable_path = local_mac_ffmpeg_path
        else:
            print(f"[DEBUG Pathing] Local macOS Homebrew ffmpeg not found or not executable at: {local_mac_ffmpeg_path}", flush=True)
    elif is_on_vercel:
        print(f"[DEBUG Pathing] On Vercel, so skipping Homebrew check or bundled ffmpeg was prioritized.", flush=True)


    # Priority 3: Fallback to 'ffmpeg' in PATH (if no specific path worked)
    if not ffmpeg_executable_path:
        print(f"[DEBUG Pathing] No specific ffmpeg path found after all checks. Using fallback 'ffmpeg' from system PATH.", flush=True)
        # To verify if 'ffmpeg' from PATH works, we'd ideally run a quick --version check here,
        # but for simplicity, we just return 'ffmpeg' and let the main command fail if it's not truly available.
        # A simple check could be `subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)`
        # However, to avoid calling subprocess within get_ffmpeg_path for now:
        return 'ffmpeg' 
    
    if not ffmpeg_executable_path: # Should ideally not be reached if fallback to 'ffmpeg' is always set
        print("[ERROR Pathing] FFmpeg path could not be determined after all checks!", flush=True)
        return None # Or raise an error

    print(f"[DEBUG Pathing] Final ffmpeg path determined: {ffmpeg_executable_path}", flush=True)
    return ffmpeg_executable_path

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def extract_brand_name(ad_title):
    """
    Attempts to extract a concise brand name from the AdTitle.
    Example: "250415_OMD_The Home Depot_HD Home Awareness Q2'25_Element+PMEF" -> "The Home Depot"
    """
    if not ad_title:
        return "Default Brand"
    
    # Try specific pattern first like "_OMD_Brand Name_"
    match_omd = re.search(r"_OMD_([^_]+)_", ad_title, re.IGNORECASE)
    if match_omd:
        return match_omd.group(1)

    # Try pattern like "Advertiser_BrandName_Campaign"
    parts = ad_title.split('_')
    if len(parts) > 1:
         # Potentially, the brand name could be the second part if the first is an ID/Advertiser
         # Or if there are multiple parts, it might be one of the earlier, more prominent ones.
         # This is a heuristic and might need adjustment based on common AdTitle formats.
        if len(parts[1]) > 2 : # Avoid short codes
            return parts[1] 

    # Fallback: look for a sequence of capitalized words (potential brand name)
    # This is a bit more complex and might require more sophisticated NLP or regex.
    # For now, let's try to find a part of the string that looks like a brand.
    # A simple heuristic: take the longest part between underscores or at the start.
    potential_brands = [p for p in parts if len(p) > 3 and p[0].isupper()]
    if potential_brands:
        return potential_brands[0] # Take the first one for now

    # If no underscores, or other methods fail, return a cleaned up version or the original
    return ad_title.split('(')[0].strip() # Basic cleanup

def extract_click_url(clickthrough_url):
    """
    Extract the embedded click URL from the clickthrough tracker.
    """
    if not clickthrough_url: return None
    try:
        parsed = urlparse(clickthrough_url)
        query_params = parse_qs(parsed.query)
        click_url_list = query_params.get('click', [])
        if click_url_list:
            click_url = click_url_list[0]
            if not (click_url.startswith('http://') or click_url.startswith('https://')) and '%%CLICK_URL_UNESC%%' in clickthrough_url:
                 pass
            return unquote(click_url)
        for key in ['u', 'url', 'redirect_url', 'destination_url', 'finalUrl']:
            dest_url_list = query_params.get(key, [])
            if dest_url_list:
                return unquote(dest_url_list[0])
    except Exception as e:
        print(f"Error parsing or extracting click URL from {clickthrough_url}: {e}")
    return None

def resolve_final_url(url, max_redirects=10):
    """
    Follow HTTP redirects to resolve the final destination URL.
    """
    if not url or not (url.startswith('http://') or url.startswith('https://')):
        print(f"Invalid URL for resolution: {url}")
        return url
    session = requests.Session()
    session.max_redirects = max_redirects
    try:
        response = session.get(url, allow_redirects=True, timeout=20)
        return response.url
    except requests.RequestException as e:
        print(f"Failed to resolve URL {url}: {e}")
        return url

def get_final_destination(clickthrough_url):
    """
    Orchestrates extraction and resolution of the final destination URL.
    Returns the final resolved URL, or the original clickthrough_url if steps fail.
    """
    if not clickthrough_url:
        return clickthrough_url

    print(f"Original ClickThrough: {clickthrough_url}")
    intermediate_url = extract_click_url(clickthrough_url)
    
    if intermediate_url:
        print(f"Extracted intermediate URL: {intermediate_url}")
        final_url = resolve_final_url(intermediate_url)
        if final_url:
            print(f"Resolved final URL: {final_url}")
            return final_url
        else: # resolve_final_url failed, return intermediate
            print(f"Failed to resolve intermediate URL, returning it: {intermediate_url}")
            return intermediate_url
    else: # No intermediate URL could be extracted
        print(f"No intermediate URL extracted, trying to resolve original: {clickthrough_url}")
        # If no specific 'click=' param, the raw_clickthrough_url might be the one to resolve directly
        final_url = resolve_final_url(clickthrough_url)
        if final_url and final_url != clickthrough_url : # Check if resolution actually changed something
            print(f"Resolved original to final URL: {final_url}")
            return final_url
        else: # Resolution failed or didn't change anything, return original
            print(f"Failed to resolve or no change from original, returning: {clickthrough_url}")
            return clickthrough_url

@app.route('/', methods=['GET', 'POST'])
def index():
    print("########## FUNCTION START - LATEST APP.PY IS RUNNING ##########", flush=True)
    # Ensure APP_DIR is accessible; it's defined globally but let's log its value here too.
    # APP_DIR = os.path.dirname(os.path.abspath(__file__)) # This is the global definition
    # print(f"[DEBUG INDEX SCOPE] APP_DIR is: {APP_DIR}", flush=True)

    if request.method == 'POST':
        print("########## POST REQUEST RECEIVED ##########", flush=True)
        vast_content = ""
        vast_input = request.form.get('vast_input', '').strip()
        vast_file = request.files.get('vast_file')

        if vast_file and vast_file.filename != '' and allowed_file(vast_file.filename):
            filename = secure_filename(vast_file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            vast_file.save(filepath)
            with open(filepath, 'r', encoding='utf-8') as f:
                vast_content = f.read()
        elif vast_input:
            if vast_input.startswith(('http://', 'https://')):
                try:
                    response = requests.get(vast_input, timeout=10)
                    response.raise_for_status() # Check for HTTP errors
                    vast_content = response.text
                except requests.RequestException as e:
                    return render_template('index.html', error=f"Error fetching VAST URL: {e}")
            else:
                vast_content = vast_input # Assume it's XML content directly

        if not vast_content:
            return render_template('index.html', error="No VAST content provided or file type not allowed.")

        try:
            # Parse VAST XML
            root = ET.fromstring(vast_content)
            
            # Find AdTitle
            ad_title_element = root.find('.//AdTitle')
            ad_title = ad_title_element.text.strip() if ad_title_element is not None and ad_title_element.text else "Untitled Ad"
            
            brand_name = extract_brand_name(ad_title)

            # Find MediaFile (MP4)
            media_file_url = None
            for mf_element in root.findall('.//MediaFile'):
                if mf_element.get('type') == 'video/mp4' and mf_element.text:
                    media_file_url = mf_element.text.strip()
                    # Prioritize by bitrate if available, higher is better
                    # For simplicity, taking the first MP4 found. Add bitrate logic if needed.
                    break 
            
            if not media_file_url:
                return render_template('index.html', error="Could not find a suitable MP4 MediaFile in VAST.")

            # Find ClickThrough URL
            clickthrough_url_element = root.find('.//ClickThrough')
            raw_clickthrough_url = clickthrough_url_element.text.strip() if clickthrough_url_element is not None and clickthrough_url_element.text else None

            if not raw_clickthrough_url:
                 return render_template('index.html', error="Could not find ClickThrough URL in VAST.")

            # Process the raw_clickthrough_url to get the final destination
            # This 'final_clickthrough_url' will be used for display and QR (if different from raw)
            # The new get_final_destination function handles extraction and resolution
            final_resolved_url = get_final_destination(raw_clickthrough_url)

            # Generate QR Code
            # QR code should always use the raw_clickthrough_url to ensure trackers are hit
            qr_filename = "qrcode.png"
            qr_filepath = os.path.join(app.config['GENERATED_FOLDER'], qr_filename)
            qr_img = qrcode.make(raw_clickthrough_url) 
            qr_img.save(qr_filepath)
            
            output_filename = f"output_{secure_filename(brand_name)}_{os.urandom(4).hex()}.mp4"
            output_filepath = os.path.join(app.config['GENERATED_FOLDER'], output_filename)
            ffmpeg_log_filepath = os.path.join(app.config['GENERATED_FOLDER'], f"{output_filename}.log")
            
            background_image_path = os.path.join(app.root_path, 'static/images/background-kerv.jpg')
            # Use a generic font name; Vercel's environment might have Arial or a substitute.
            # A more robust solution is to bundle a .ttf file.
            font_path = "Arial" 
            # if not os.path.exists(font_path): # This check is not reliable for generic names
            #     font_path = "Arial" # Fallback already set

            def escape_ffmpeg_text(text):
                # Reverted to the version that was stable previously
                return text.replace("'", "\\\\\\'").replace(":", "\\\\:").replace("%", "\\\\%")

            # --- Reverted URL for display in video (strip scheme, no '@') ---
            url_for_display_text = final_resolved_url if final_resolved_url else raw_clickthrough_url
            
            decoded_for_video_display = unquote(url_for_display_text)
            text_to_draw_for_url = decoded_for_video_display.replace('https://','').replace('http://','')
            
            if len(text_to_draw_for_url) > 70: 
                text_to_draw_for_url = text_to_draw_for_url[:67] + "..."
            
            simplified_url_for_display = text_to_draw_for_url
            # --- End of reverted URL logic ---

            # VAST video (input 2) framerate. Default to 23.98 if not discoverable, but actual video might vary.
            # For simplicity, hardcoding 23.98. A more robust solution might probe the video.
            video_framerate = "23.98"
            cta_text = "SCAN QR CODE FOR MORE." # Uppercase CTA

            filter_complex_str = (
                "[0:v]scale=1920:1080[base_bg];"  # Input 0 is background
                "[1:v]scale=530:530[scaled_qr];"  # Input 1 is QR code, scaled to 530x530
                "[2:v]scale=1164:654[scaled_ad_video];" # Input 2 is VAST ad video, scaled to 1164x654
                "[base_bg][scaled_ad_video]overlay=x=80:y=163[video_on_bg];" # VAST video position
                "[video_on_bg][scaled_qr]overlay=x=1317:y=163:shortest=1[with_qr];" # QR code position
                # Draw texts on the [with_qr] stream
                f"[with_qr]"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(brand_name)}':fontcolor=white:fontsize=45:x=80:y=857,"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(simplified_url_for_display)}':fontcolor=white:fontsize=30:x=80:y=917,"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(cta_text)}':fontcolor=white:fontsize=38:x=1332:y=723[final_output]" # Ensure [final_output] is here
            )

            # Remove temp file for filter script
            # filter_script_file = tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt', dir=app.config['GENERATED_FOLDER'])
            # filter_script_file.write(filter_complex_str)
            # filter_script_filepath = filter_script_file.name
            # filter_script_file.close()
            # print(f"Filter script path: {filter_script_filepath}") # DEBUG
            # print(f"Filter script content:\\n{filter_complex_str}") # DEBUG

            ffmpeg_path = get_ffmpeg_path() # Assuming get_ffmpeg_path() is defined and works
            if not ffmpeg_path:
                # This was present in the user's previous version and seems like good error handling
                return render_template('index.html', error="FFmpeg not found. Please ensure it is installed and accessible.")

            # Construct the FFmpeg command
            ffmpeg_command = [
                ffmpeg_path,
                '-y',  # Overwrite output files without asking
                # Inputs:
                '-i', background_image_path,      # Input 0: Background image
                '-i', qr_filepath,                # Input 1: QR code image
                '-i', media_file_url,             # Input 2: VAST ad video
                # Filter complex directly
                '-filter_complex', filter_complex_str,
                # Mapping:
                '-map', '[final_output]',         # Map the final output of the filter complex to video
                '-map', '2:a?',                   # Map audio from VAST ad video (input 2), if it exists
                # Video Codec
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',                     # Constant Rate Factor (quality)
                # Audio Codec
                '-c:a', 'aac',
                '-b:a', '192k',
                # Other options
                '-r', video_framerate,            # Set output framerate
                '-shortest',                      # Finish encoding when the shortest input stream ends
                '-movflags', '+faststart',        # For web playback (streamable)
                output_filepath
            ]
            
            print(f"FFMPEG Command: {' '.join(shlex.quote(str(arg)) for arg in ffmpeg_command)}", flush=True)

            # Ensure the directory for the log file exists (was present in previous version)
            os.makedirs(os.path.dirname(ffmpeg_log_filepath), exist_ok=True)
            
            process = None # Initialize process
            try:
                # Using Popen with communicate, text=True, and errors='replace'
                with open(ffmpeg_log_filepath, 'w') as log_file_handle: # Renamed to avoid conflict if log_file is used elsewhere
                    process = subprocess.Popen(
                        ffmpeg_command,
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.PIPE, # Capture stderr
                        text=True,
                        errors='replace' # Prevents decoding errors on unknown characters
                    )
                    stdout, stderr = process.communicate(timeout=120) # 120 seconds timeout

                    # Write to log file *after* communicate
                    log_file_handle.write(f"FFMPEG COMMAND: {' '.join(shlex.quote(str(arg)) for arg in ffmpeg_command)}\\n")
                    log_file_handle.write(f"FFMPEG process.returncode: {process.returncode}\\n")
                    log_file_handle.write(f"FFMPEG STDOUT:\\n{stdout}\\n")
                    log_file_handle.write(f"FFMPEG STDERR:\\n{stderr}\\n")

                if process.returncode == 0:
                    # Success: clear any previous error from the template context
                    return render_template('index.html', 
                                           video_url=url_for('generated_file', filename=output_filename), 
                                           download_url=url_for('generated_file', filename=output_filename),
                                           ffmpeg_stderr=None) # Clear previous stderr
                else:
                    # FFmpeg failed
                    print(f"FFMPEG Popen STDOUT (on error):\n{stdout}", flush=True) # Log to Vercel
                    print(f"FFMPEG Popen STDERR (on error):\n{stderr}", flush=True) # Log to Vercel
                    
                    detailed_error = f"FFmpeg processing failed. RC: {process.returncode}.\\n"
                    if stdout and stdout.strip(): # Add stdout if not empty
                        detailed_error += f"STDOUT:\\n{stdout}\\n"
                    if stderr and stderr.strip(): # Add stderr if not empty
                        detailed_error += f"STDERR:\\n{stderr}\\n"
                    else: # If stderr is empty, mention it could be in the log or a different issue
                        detailed_error += "STDERR was empty. Check Vercel logs or FFmpeg log file for more details if the issue persists.\\n"
                    
                    # Attempt to read the full log file content as a fallback for display
                    ffmpeg_log_content_for_display = ""
                    if os.path.exists(ffmpeg_log_filepath):
                        with open(ffmpeg_log_filepath, 'r') as log_f_display:
                            ffmpeg_log_content_for_display = log_f_display.read()
                        # No need to append to detailed_error if stderr was already captured and displayed.
                        # The log file written above already contains stdout & stderr.
                        # However, if stderr was empty, the log file might have more.
                        if not (stderr and stderr.strip()):
                             detailed_error += f"Full FFmpeg log from {ffmpeg_log_filepath}:\\n{ffmpeg_log_content_for_display[:2000]}"


                    return render_template('index.html', error=detailed_error, ffmpeg_stderr=stderr) # Pass stderr to template

            except subprocess.TimeoutExpired:
                print("FFmpeg process timed out after 120 seconds.", flush=True)
                if process:
                    process.kill()
                    # Attempt to get output after kill (might be partial or None)
                    stdout, stderr = process.communicate() 
                    print(f"FFMPEG Popen STDOUT (timeout):\n{stdout}", flush=True)
                    print(f"FFMPEG Popen STDERR (timeout):\n{stderr}", flush=True)
                
                # Try to read log file on timeout
                ffmpeg_log_content_on_timeout = ""
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as log_f_timeout:
                        ffmpeg_log_content_on_timeout = log_f_timeout.read()

                error_message = "FFmpeg processing timed out after 120 seconds."
                if ffmpeg_log_content_on_timeout:
                    error_message += f"\\nPartial log:\\n{ffmpeg_log_content_on_timeout[:2000]}"
                else:
                    error_message += "\\nNo FFmpeg log content found."

                return render_template('index.html', error=error_message, ffmpeg_stderr=stderr if stderr else "Timeout, no stderr captured.")
            except Exception as e:
                print(f"An unexpected error occurred during FFmpeg processing: {e}", flush=True)
                
                # Try to read log file on general exception
                ffmpeg_log_content_on_exception = ""
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as log_f_exception:
                        ffmpeg_log_content_on_exception = log_f_exception.read()
                
                error_message = f"An unexpected error occurred: {str(e)}"
                if ffmpeg_log_content_on_exception:
                     error_message += f"\\nFFmpeg log may contain details:\\n{ffmpeg_log_content_on_exception[:2000]}"

                return render_template('index.html', error=error_message, ffmpeg_stderr="Exception, check logs.")

        except ET.ParseError:
            return render_template('index.html', error="Invalid XML content in VAST tag.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            return render_template('index.html', error=f"An unexpected error occurred: {e}")

    return render_template('index.html')

@app.route('/generated/<filename>')
def generated_file(filename):
    return send_from_directory(app.config['GENERATED_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001) 
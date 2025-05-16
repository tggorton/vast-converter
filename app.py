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
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)

# Flask App Initialization
app = Flask(__name__)

# Configuration using environment variables
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default_secret_key')
app.config['DEBUG'] = os.environ.get('DEBUG', 'False') == 'True'
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 30 * 1024 * 1024))  # 30 MB default

# Configuration
APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_DIR, 'uploads')
GENERATED_FOLDER = os.path.join(APP_DIR, 'generated')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['GENERATED_FOLDER'] = GENERATED_FOLDER

# Ensure upload and generated directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)

def get_ffmpeg_path():
    ffmpeg_executable_path = ''
    print(f"[DEBUG Pathing - LOCAL] get_ffmpeg_path invoked. APP_DIR: {APP_DIR}", flush=True)

    local_mac_ffmpeg_path = '/opt/homebrew/bin/ffmpeg'
    print(f"[DEBUG Pathing - LOCAL] Checking for local macOS Homebrew ffmpeg at: {local_mac_ffmpeg_path}", flush=True)
    if os.path.exists(local_mac_ffmpeg_path) and os.access(local_mac_ffmpeg_path, os.X_OK):
        print(f"[DEBUG Pathing - LOCAL] Using local macOS ffmpeg (Homebrew): {local_mac_ffmpeg_path}", flush=True)
        ffmpeg_executable_path = local_mac_ffmpeg_path
    else:
        print(f"[DEBUG Pathing - LOCAL] Local macOS Homebrew ffmpeg not found or not executable at: {local_mac_ffmpeg_path}. Attempting fallback to PATH.", flush=True)

    if not ffmpeg_executable_path:
        # Fallback: 'ffmpeg' from system PATH
        print(f"[DEBUG Pathing - LOCAL] Trying 'ffmpeg' from system PATH.", flush=True)
        ffmpeg_executable_path = 'ffmpeg'
    
    # Validate the chosen ffmpeg path by trying to run it
    if ffmpeg_executable_path: 
        try:
            print(f"[DEBUG Pathing - LOCAL] Attempting to get version from: {ffmpeg_executable_path}", flush=True)
            process = subprocess.run([ffmpeg_executable_path, '-version'], capture_output=True, text=True, timeout=5, check=False)
            print(f"[DEBUG Pathing - LOCAL] {ffmpeg_executable_path} -version RC: {process.returncode}", flush=True)
            if process.stdout or process.stderr: 
                 print(f"[DEBUG Pathing - LOCAL] {ffmpeg_executable_path} -version STDOUT snippet: {process.stdout[:200]}", flush=True)
                 print(f"[DEBUG Pathing - LOCAL] {ffmpeg_executable_path} -version STDERR snippet: {process.stderr[:200]}", flush=True)
                 # Consider valid if it runs and produces output, even if RC is not 0 for -version
            else: 
                print(f"[ERROR Pathing - LOCAL] {ffmpeg_executable_path} -version produced no output. Path may be invalid or ffmpeg corrupted.", flush=True)
                ffmpeg_executable_path = '' # Invalidate path
        except subprocess.TimeoutExpired:
            print(f"[ERROR Pathing - LOCAL] Timeout when trying {ffmpeg_executable_path} -version. Path: {ffmpeg_executable_path}", flush=True)
            ffmpeg_executable_path = '' 
        except Exception as e_version:
            print(f"[ERROR Pathing - LOCAL] Exception while trying {ffmpeg_executable_path} -version: {e_version}. Path: {ffmpeg_executable_path}", flush=True)
            ffmpeg_executable_path = ''

    if not ffmpeg_executable_path:
        print("[ERROR Pathing - LOCAL] FFmpeg path could not be determined or validated. Please ensure ffmpeg is installed and in your PATH, or at /opt/homebrew/bin/ffmpeg.", flush=True)
        return None 

    print(f"[DEBUG Pathing - LOCAL] Final ffmpeg path determined: {ffmpeg_executable_path}", flush=True)
    return ffmpeg_executable_path

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def extract_brand_name(ad_title):
    if not ad_title:
        return "Default Brand"
    match_omd = re.search(r"_OMD_([^_]+)_", ad_title, re.IGNORECASE)
    if match_omd:
        return match_omd.group(1)
    parts = ad_title.split('_')
    if len(parts) > 1:
        if len(parts[1]) > 2: 
            return parts[1]
    potential_brands = [p for p in parts if len(p) > 3 and p[0].isupper()]
    if potential_brands:
        return potential_brands[0]
    return ad_title.split('(')[0].strip()

def extract_click_url(clickthrough_url):
    if not clickthrough_url: return None
    try:
        parsed = urlparse(clickthrough_url)
        query_params = parse_qs(parsed.query)
        click_url_list = query_params.get('click', [])
        if click_url_list:
            click_url = click_url_list[0]
            return unquote(click_url)
        for key in ['u', 'url', 'redirect_url', 'destination_url', 'finalUrl', 'targetUrl', 'goto']:
            dest_url_list = query_params.get(key, [])
            if dest_url_list:
                return unquote(dest_url_list[0])
    except Exception as e:
        print(f"Error parsing or extracting click URL from {clickthrough_url}: {e}", flush=True)
    return None

def resolve_final_url(url, max_redirects=10, timeout_seconds=10):
    if not url or not (url.startswith('http://') or url.startswith('https://')):
        print(f"Invalid URL for resolution: {url}", flush=True)
        return url
    session = requests.Session()
    session.max_redirects = max_redirects
    try:
        response = session.get(url, allow_redirects=True, timeout=timeout_seconds)
        return response.url
    except requests.exceptions.Timeout:
        print(f"Timeout ({timeout_seconds}s) resolving URL {url}", flush=True)
        return url
    except requests.RequestException as e:
        print(f"Failed to resolve URL {url}: {e}", flush=True)
        return url

def get_final_destination(clickthrough_url):
    if not clickthrough_url:
        return clickthrough_url
    print(f"Original ClickThrough: {clickthrough_url}", flush=True)
    intermediate_url = extract_click_url(clickthrough_url)
    
    if intermediate_url:
        print(f"Extracted intermediate URL: {intermediate_url}", flush=True)
        final_url = resolve_final_url(intermediate_url)
        if final_url and final_url != intermediate_url:
            print(f"Resolved final URL: {final_url}", flush=True)
            return final_url
        else:
            print(f"Failed to resolve intermediate URL or no change, returning intermediate: {intermediate_url}", flush=True)
            return intermediate_url
    else:
        print(f"No intermediate URL extracted, trying to resolve original: {clickthrough_url}", flush=True)
        final_url = resolve_final_url(clickthrough_url)
        if final_url and final_url != clickthrough_url:
            print(f"Resolved original to final URL: {final_url}", flush=True)
            return final_url
        else:
            print(f"Failed to resolve or no change from original, returning original: {clickthrough_url}", flush=True)
            return clickthrough_url

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/', methods=['GET', 'POST'])
def index():
    logging.debug("########## FUNCTION START - CLEAN LOCAL APP.PY ##########")
    if request.method == 'POST':
        logging.debug("########## POST REQUEST RECEIVED (CLEAN LOCAL) ##########")
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
                    response.raise_for_status()
                    vast_content = response.text
                except requests.RequestException as e:
                    return render_template('index.html', error=f"Error fetching VAST URL: {e}")
            else:
                vast_content = vast_input 

        if not vast_content:
            return render_template('index.html', error="No VAST content provided or file type not allowed.")

        try:
            root = ET.fromstring(vast_content)
            ad_title_element = root.find('.//AdTitle')
            ad_title = ad_title_element.text.strip() if ad_title_element is not None and ad_title_element.text else "Untitled Ad"
            brand_name = extract_brand_name(ad_title)

            media_file_url = None
            for mf_element in root.findall('.//MediaFile'):
                if mf_element.get('type') == 'video/mp4' and mf_element.text:
                    media_file_url = mf_element.text.strip()
                    break
            if not media_file_url:
                return render_template('index.html', error="Could not find a suitable MP4 MediaFile in VAST.", vast_content_snippet=vast_content[:1000])

            clickthrough_url_element = root.find('.//ClickThrough')
            raw_clickthrough_url = clickthrough_url_element.text.strip() if clickthrough_url_element is not None and clickthrough_url_element.text else None

            if not raw_clickthrough_url:
                click_tracking_elements = root.findall('.//ClickTracking')
                if click_tracking_elements:
                    raw_clickthrough_url = click_tracking_elements[0].text.strip() if click_tracking_elements[0].text else None
                    print(f"Found ClickTracking as fallback: {raw_clickthrough_url}", flush=True)

            if not raw_clickthrough_url:
                 return render_template('index.html', error="Could not find ClickThrough or ClickTracking URL in VAST.", ad_title=ad_title, brand_name=brand_name, media_file_url=media_file_url, vast_content_snippet=vast_content[:1000])

            final_resolved_url = get_final_destination(raw_clickthrough_url)

            qr_filename = "qrcode.png"
            qr_filepath = os.path.join(app.config['GENERATED_FOLDER'], qr_filename)
            qr_img = qrcode.make(raw_clickthrough_url)
            qr_img.save(qr_filepath)
            
            output_filename = f"output_{secure_filename(brand_name)}_{os.urandom(4).hex()}.mp4"
            output_filepath = os.path.join(app.config['GENERATED_FOLDER'], output_filename)
            ffmpeg_log_filepath = os.path.join(app.config['GENERATED_FOLDER'], f"{output_filename}.log")
            
            background_image_path = os.path.join(APP_DIR, 'static/images/background-kerv.jpg')
            font_path = "Arial" # Using Arial, common on macOS

            def escape_ffmpeg_text(text):
                return text.replace("'", "\\\\\\'").replace(":", "\\\\:").replace("%", "\\\\%")
            
            url_for_display_text = final_resolved_url if final_resolved_url and final_resolved_url != raw_clickthrough_url else raw_clickthrough_url
            decoded_for_video_display = unquote(url_for_display_text)
            text_to_draw_for_url = decoded_for_video_display.replace('https://','').replace('http://','')
            if len(text_to_draw_for_url) > 70:
                text_to_draw_for_url = text_to_draw_for_url[:67] + "..."
            simplified_url_for_display = text_to_draw_for_url

            video_framerate = "23.98"
            cta_text = "SCAN QR CODE FOR MORE."

            filter_complex_str = (
                "[0:v]scale=1920:1080[base_bg];"
                "[1:v]scale=530:530[scaled_qr];"
                "[2:v]scale=1164:654[scaled_ad_video];"
                "[base_bg][scaled_ad_video]overlay=x=80:y=163[video_on_bg];"
                "[video_on_bg][scaled_qr]overlay=x=1317:y=163:shortest=1[with_qr];"
                f"[with_qr]"
                f"drawtext=fontfile='{font_path}':text='{escape_ffmpeg_text(brand_name)}':fontcolor=white:fontsize=45:x=80:y=857,"
                f"drawtext=fontfile='{font_path}':text='{escape_ffmpeg_text(simplified_url_for_display)}':fontcolor=white:fontsize=30:x=80:y=917,"
                f"drawtext=fontfile='{font_path}':text='{escape_ffmpeg_text(cta_text)}':fontcolor=white:fontsize=38:x=1332:y=723[final_output]"
            )

            filter_script_file = tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt', dir=app.config['GENERATED_FOLDER'])
            filter_script_file.write(filter_complex_str)
            filter_script_filepath = filter_script_file.name
            filter_script_file.close()
            print(f"Filter script path: {filter_script_filepath}", flush=True)
            print(f"Filter script content:\\n{filter_complex_str}", flush=True)

            ffmpeg_path = get_ffmpeg_path()
            if not ffmpeg_path:
                os.remove(filter_script_filepath)
                return render_template('index.html', error="FFmpeg not found or not validated. Please ensure it is installed and accessible via /opt/homebrew/bin/ffmpeg or your system PATH.",
                                       ad_title=ad_title, brand_name=brand_name, media_file_url=media_file_url, raw_clickthrough_url=raw_clickthrough_url,
                                       final_resolved_url=final_resolved_url, qr_code_url=url_for('generated_file', filename=qr_filename), vast_content_snippet=vast_content[:1000])

            ffmpeg_command = [
                ffmpeg_path, '-y',
                '-loglevel', 'debug',
                '-loop', '1', '-r', video_framerate, '-i', background_image_path,
                '-loop', '1', '-r', video_framerate, '-i', qr_filepath,
                '-i', media_file_url,
                '-filter_complex_script', filter_script_filepath,
                '-map', '[final_output]',
                '-map', '2:a?',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '192k',
                '-pix_fmt', 'yuv420p',
                '-r', video_framerate, '-shortest', '-movflags', '+faststart',
                output_filepath
            ]
            ffmpeg_command_str = ' '.join(shlex.quote(str(arg)) for arg in ffmpeg_command)
            print(f"FFMPEG Command: {ffmpeg_command_str}", flush=True)
            
            os.makedirs(os.path.dirname(ffmpeg_log_filepath), exist_ok=True)
            process = None
            ffmpeg_log_content = ""
            stdout_ffmpeg = ""
            stderr_ffmpeg = ""

            try:
                with open(ffmpeg_log_filepath, 'w') as log_file_handle:
                    process = subprocess.Popen(
                        ffmpeg_command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        errors='replace'
                    )
                    stdout_ffmpeg, stderr_ffmpeg = process.communicate(timeout=120)
                    
                    log_file_handle.write(f"FFMPEG COMMAND: {ffmpeg_command_str}\\n")
                    log_file_handle.write(f"FFMPEG process.returncode: {process.returncode}\\n")
                    log_file_handle.write(f"FFMPEG STDOUT:\\n{stdout_ffmpeg}\\n")
                    log_file_handle.write(f"FFMPEG STDERR:\\n{stderr_ffmpeg}\\n")

                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as log_f_display:
                        ffmpeg_log_content = log_f_display.read()

                base_context = {
                    'ad_title': ad_title,
                    'brand_name': brand_name,
                    'media_file_url': media_file_url,
                    'raw_clickthrough_url': raw_clickthrough_url,
                    'final_resolved_url': final_resolved_url,
                    'qr_code_url': url_for('generated_file', filename=qr_filename),
                    'ffmpeg_command_str': ffmpeg_command_str,
                    'ffmpeg_log_content': ffmpeg_log_content,
                    'vast_content_snippet': vast_content[:1000] # For debugging in template if needed
                }

                if process.returncode == 0:
                    return render_template('index.html', 
                                           video_url=url_for('generated_file', filename=output_filename), 
                                           download_url=url_for('generated_file', filename=output_filename),
                                           output_filename=output_filename,
                                           **base_context)
                else:
                    detailed_error = f"FFmpeg processing failed. RC: {process.returncode}."
                    # STDOUT and STDERR are already in ffmpeg_log_content from the file read
                    return render_template('index.html', error=detailed_error, ffmpeg_stderr=stderr_ffmpeg, **base_context)
            except subprocess.TimeoutExpired:
                if process: 
                    process.kill()
                    # Try to get any final output
                    stdout_timeout, stderr_timeout = process.communicate() 
                    stdout_ffmpeg += "\nTIMEOUT OCCURRED - Partial STDOUT from timeout:\n" + stdout_timeout
                    stderr_ffmpeg += "\nTIMEOUT OCCURRED - Partial STDERR from timeout:\n" + stderr_timeout
                    # Update log file with timeout info if possible
                    if os.path.exists(ffmpeg_log_filepath):
                        with open(ffmpeg_log_filepath, 'a') as log_file_handle: # Append timeout info
                            log_file_handle.write("\n\n--- TIMEOUT OCCURRED ---\n")
                            log_file_handle.write(f"Partial STDOUT after kill:\n{stdout_timeout}")
                            log_file_handle.write(f"Partial STDERR after kill:\n{stderr_timeout}")
                
                # Re-read log content after potential append
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as log_f_timeout_display:
                        ffmpeg_log_content = log_f_timeout_display.read()

                error_message = "FFmpeg processing timed out after 120 seconds."
                base_context_timeout = {
                    'ad_title': ad_title, 'brand_name': brand_name, 'media_file_url': media_file_url,
                    'raw_clickthrough_url': raw_clickthrough_url, 'final_resolved_url': final_resolved_url,
                    'qr_code_url': url_for('generated_file', filename=qr_filename), 'ffmpeg_command_str': ffmpeg_command_str,
                    'ffmpeg_log_content': ffmpeg_log_content, 'vast_content_snippet': vast_content[:1000]
                }
                return render_template('index.html', error=error_message, ffmpeg_stderr=stderr_ffmpeg, **base_context_timeout)
            except Exception as e_ffmpeg:
                error_message = f"An unexpected error occurred during FFmpeg subprocess: {str(e_ffmpeg)}"
                # Re-read log content if it exists
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as log_f_ex_display:
                        ffmpeg_log_content = log_f_ex_display.read()
                else:
                    ffmpeg_log_content = "FFmpeg log file not found or not created before exception."
                
                base_context_exception = {
                    'ad_title': ad_title, 'brand_name': brand_name, 'media_file_url': media_file_url,
                    'raw_clickthrough_url': raw_clickthrough_url, 'final_resolved_url': final_resolved_url,
                    'qr_code_url': url_for('generated_file', filename=qr_filename), 
                    'ffmpeg_command_str': ffmpeg_command_str if 'ffmpeg_command_str' in locals() else "FFmpeg command not prepared.",
                    'ffmpeg_log_content': ffmpeg_log_content, 'vast_content_snippet': vast_content[:1000]
                }
                return render_template('index.html', error=error_message, ffmpeg_stderr=str(e_ffmpeg), **base_context_exception)
            finally:
                if os.path.exists(filter_script_filepath):
                    try:
                        os.remove(filter_script_filepath)
                    except OSError as e_remove:
                        print(f"Error removing temp filter script {filter_script_filepath}: {e_remove}", flush=True)

        except ET.ParseError as e_xml:
            return render_template('index.html', error=f"Invalid XML content in VAST tag: {e_xml}", vast_content_snippet=vast_content[:1000])
        except Exception as e_main:
            logging.error(f"An unexpected error occurred: {e_main}")
            import traceback
            tb_str = traceback.format_exc()
            print(tb_str, flush=True)
            # Try to pass any VAST related data if it was extracted before the error
            context_on_general_error = {
                'error': f"An unexpected error occurred: {e_main} traceback: {tb_str[:1000]}",
                'ad_title': ad_title if 'ad_title' in locals() else None,
                'brand_name': brand_name if 'brand_name' in locals() else None,
                'media_file_url': media_file_url if 'media_file_url' in locals() else None,
                'raw_clickthrough_url': raw_clickthrough_url if 'raw_clickthrough_url' in locals() else None,
                'final_resolved_url': final_resolved_url if 'final_resolved_url' in locals() else None,
                'qr_code_url': url_for('generated_file', filename=qr_filename) if 'qr_code_url' in locals() else None,
                'vast_content_snippet': vast_content[:1000] if 'vast_content' in locals() else "VAST content not available."
            }
            return render_template('index.html', **context_on_general_error)

    return render_template('index.html')

@app.route('/generated/<filename>')
def generated_file(filename):
    safe_generated_folder = os.path.abspath(app.config['GENERATED_FOLDER'])
    file_path = os.path.abspath(os.path.join(safe_generated_folder, filename))
    if not file_path.startswith(safe_generated_folder):
        print(f"[SECURITY] Attempt to access file outside generated folder: {file_path} (based on {filename})", flush=True)
        return "Invalid path", 400
    print(f"Serving generated file from: {app.config['GENERATED_FOLDER']} for filename: {filename}", flush=True)
    return send_from_directory(app.config['GENERATED_FOLDER'], filename)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    print(f"Starting Flask dev server locally on 127.0.0.1:{port} with reloader DISABLED.", flush=True)
    app.run(host='127.0.0.1', port=port, debug=True, use_reloader=False)
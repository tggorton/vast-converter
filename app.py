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
app.config['UPLOAD_FOLDER'] = '/Users/grantgorton/Desktop/Projects/vast-to-ctv/uploads'
app.config['GENERATED_FOLDER'] = '/Users/grantgorton/Desktop/Projects/vast-to-ctv/generated'
app.config['ALLOWED_EXTENSIONS'] = {'xml', 'txt'}

# Ensure generated and uploads folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['GENERATED_FOLDER'], exist_ok=True)

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
    print("########## LATEST APP.PY IS RUNNING ##########") # Debug print
    if request.method == 'POST':
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
            font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
            if not os.path.exists(font_path):
                font_path = "Arial"

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
                "[base_bg][scaled_ad_video]overlay=x=80:y=163[video_on_bg];" # VAST video position (y shifted up by 50px)
                "[video_on_bg][scaled_qr]overlay=x=1317:y=163:shortest=1[with_qr];" # QR code centered in L-bar, top-aligned with video (y shifted up by 50px)
                # Draw texts on the [with_qr] stream
                f"[with_qr]"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(brand_name)}':fontcolor=white:fontsize=45:x=80:y=857,"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(simplified_url_for_display)}':fontcolor=white:fontsize=30:x=80:y=917,"
                f"drawtext=fontfile={shlex.quote(font_path)}:text='{escape_ffmpeg_text(cta_text)}':fontcolor=white:fontsize=38:x=1332:y=723"
            )

            filter_script_file = tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt', dir=app.config['GENERATED_FOLDER'])
            filter_script_file.write(filter_complex_str)
            filter_script_filepath = filter_script_file.name
            filter_script_file.close()
            print(f"Filter script path: {filter_script_filepath}")
            print(f"Filter script content:\\n{filter_complex_str}")

            ffmpeg_command = [
                '/opt/homebrew/bin/ffmpeg', '-y',
                '-loglevel', 'debug',
                '-loop', '1', '-r', video_framerate, '-i', background_image_path,  # Input 0
                '-loop', '1', '-r', video_framerate, '-i', qr_filepath,              # Input 1
                '-i', media_file_url,                                              # Input 2 (VAST video)
                '-filter_complex_script', filter_script_filepath,
                '-c:v', 'libx264',
                '-c:a', 'copy',
                '-preset', 'fast',
                '-shortest', 
                output_filepath
            ]
            
            ffmpeg_stderr_content = ""
            try:
                project_dir = "/Users/grantgorton/Desktop/Projects/vast-to-ctv"
                process = subprocess.Popen(ffmpeg_command, cwd=project_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = process.communicate(timeout=300)

                # Write ffmpeg output to log file regardless of success/failure for inspection
                with open(ffmpeg_log_filepath, 'w') as log_file:
                    log_file.write(f"FFMPEG COMMAND: {' '.join(ffmpeg_command)}\n") # Log the command
                    log_file.write(f"FFMPEG process.returncode: {process.returncode}\n")
                    log_file.write("FFMPEG STDOUT:\n")
                    log_file.write(stdout.decode('utf-8', 'ignore'))
                    log_file.write("\n\nFFMPEG STDERR:\n")
                    log_file.write(stderr.decode('utf-8', 'ignore'))
                
                if os.path.exists(filter_script_filepath):
                    with open(filter_script_filepath, 'r') as f_filt:
                        print(f"--- Content of {filter_script_filepath} ---")
                        print(f_filt.read())
                        print("-------------------------------------------")
                    # os.remove(filter_script_filepath) # Clean up temp filter script
                
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as f_log:
                        ffmpeg_stderr_content = f_log.read()

                if process.returncode != 0:
                    print(f"FFmpeg failed with return code {process.returncode}")
                    # ffmpeg_stderr_content is already read from log file
                    return render_template('index.html', error=f"FFmpeg processing failed. RC: {process.returncode}. Check log.", ffmpeg_stderr=ffmpeg_stderr_content[:3000])
            
            except subprocess.TimeoutExpired:
                # (Error handling for timeout - fine as is, but ensure ffmpeg_stderr_content is populated)
                process.kill()
                # Try to read the log file even on timeout, it might contain partial info
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as f_log:
                        ffmpeg_stderr_content = f_log.read()
                else:
                    ffmpeg_stderr_content = "FFmpeg process timed out. Log file not found or not written."
                print("FFmpeg timeout.")
                return render_template('index.html', error="FFmpeg processing timed out (5 minutes).", ffmpeg_stderr=ffmpeg_stderr_content[:3000])
            except Exception as e:
                # (General error handling - fine as is, but ensure ffmpeg_stderr_content is populated)
                if os.path.exists(ffmpeg_log_filepath):
                    with open(ffmpeg_log_filepath, 'r') as f_log:
                        ffmpeg_stderr_content = f_log.read()
                else:
                    ffmpeg_stderr_content = f"Log file not found. Exception: {str(e)}"
                return render_template('index.html', error=f"Error during FFmpeg execution: {e}", ffmpeg_stderr=ffmpeg_stderr_content[:3000])
            
            if not os.path.exists(output_filepath) or os.path.getsize(output_filepath) == 0:
                print(f"FFmpeg output file missing or empty: {output_filepath}")
                # ffmpeg_stderr_content is already read from log file
                return render_template('index.html', error="FFmpeg completed but output file is missing or empty. Check log.", ffmpeg_stderr=ffmpeg_stderr_content[:3000])

            return render_template('index.html', 
                                   vast_content=vast_content[:1000]+"...", # Show snippet
                                   ad_title=ad_title,
                                   brand_name=brand_name,
                                   media_file_url=media_file_url,
                                   raw_clickthrough_url=raw_clickthrough_url, # Keep sending raw for info
                                   final_clickthrough_url=final_resolved_url, # Send the resolved one to template
                                   qr_code_url=url_for('generated_file', filename=qr_filename),
                                   output_video_url=url_for('generated_file', filename=output_filename),
                                   output_filename=output_filename
                                   )

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
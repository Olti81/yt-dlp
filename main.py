import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox # Added messagebox for paste error
import subprocess
import threading
import os
import sys
import platform # Import platform module

# --- Configuration ---
# Option 1: Specify the full path to yt-dlp.exe if it's not in your PATH
YT_DLP_PATH = "yt-dlp.exe"
# Option 2: Leave as "yt-dlp.exe" or "yt-dlp" if it's in your system PATH

FFMPEG_PATH = "ffmpeg.exe" # Specify path to ffmpeg if not in PATH, needed for audio extraction

# --- Default Download Directory ---
# Use the requested specific path. Using a raw string (r"...") is safer for Windows paths.
# Fallback to user's home directory if the specific path doesn't exist.
DEFAULT_DOWNLOAD_DIR = r"C:\Users\ohaal\Downloads"
if not os.path.isdir(DEFAULT_DOWNLOAD_DIR):
    print(f"Warning: Default directory '{DEFAULT_DOWNLOAD_DIR}' not found. Falling back to user's home directory.")
    DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~")
    # If home directory also doesn't work (unlikely), fallback to script directory
    if not os.path.isdir(DEFAULT_DOWNLOAD_DIR):
         DEFAULT_DOWNLOAD_DIR = os.path.dirname(os.path.abspath(__file__))


# --- Helper Function to find executable ---
def find_executable(name, specified_path):
    """Checks if an executable exists at the specified path or in the system PATH."""
    # Add .exe suffix automatically on Windows if not present
    if platform.system() == "Windows" and not specified_path.lower().endswith('.exe'):
        specified_path_exe = specified_path + ".exe"
        if os.path.exists(specified_path_exe) and os.path.isfile(specified_path_exe):
            return specified_path_exe
    # Check original specified path
    if os.path.exists(specified_path) and os.path.isfile(specified_path):
        return specified_path

    # Check if it's in PATH (add .exe on Windows for check)
    command_to_check = name
    if platform.system() == "Windows" and not command_to_check.lower().endswith('.exe'):
        command_to_check += ".exe"

    try:
        # Use the command name directly (e.g., 'yt-dlp' or 'ffmpeg')
        # The system PATH lookup will handle finding the .exe on Windows
        result = subprocess.run([name, '--version'], capture_output=True, text=True, check=True, startupinfo=get_startup_info())
        # If the command runs without error, it's likely in PATH
        return name # Return just the name, assuming it's callable directly
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Try checking with explicit .exe if on Windows and name didn't include it
        if platform.system() == "Windows" and name.lower() != command_to_check.lower():
             try:
                 result = subprocess.run([command_to_check, '--version'], capture_output=True, text=True, check=True, startupinfo=get_startup_info())
                 return command_to_check # Return name with .exe if that worked
             except (FileNotFoundError, subprocess.CalledProcessError):
                 return None # Not found
        return None # Not found

def get_startup_info():
    """Creates startupinfo structure to hide console window on Windows."""
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo
    return None

class YtdlpGui:
    def __init__(self, root):
        self.root = root
        self.root.title("yt-dlp Downloader")
        self.root.geometry("650x550") # Increased height slightly for button spacing
        self.root.resizable(True, True) # Allow resizing

        # --- Check for dependencies ---
        self.yt_dlp_executable = find_executable("yt-dlp", YT_DLP_PATH)
        self.ffmpeg_executable = find_executable("ffmpeg", FFMPEG_PATH) # Needed for audio conversion

        # --- Style ---
        self.style = ttk.Style(self.root)
        # Try different themes for better look if 'clam' isn't ideal
        available_themes = self.style.theme_names()
        if 'vista' in available_themes:
             self.style.theme_use('vista')
        elif 'clam' in available_themes:
             self.style.theme_use('clam')
        # Add more fallbacks if needed

        # --- Variables ---
        self.url_var = tk.StringVar()
        self.format_var = tk.StringVar(value="MP4 Video") # Default format
        # Variable to store the chosen output DIRECTORY
        self.output_dir_var = tk.StringVar(value=DEFAULT_DOWNLOAD_DIR)
        self.is_downloading = False

        # --- Main Frame ---
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(expand=True, fill=tk.BOTH)

        # --- Input Section ---
        input_frame = ttk.LabelFrame(main_frame, text="Input & Options", padding="10")
        input_frame.pack(fill=tk.X, pady=(0, 10))
        input_frame.columnconfigure(1, weight=1) # Make entry expand

        ttk.Label(input_frame, text="Video/Audio URL:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.url_entry = ttk.Entry(input_frame, textvariable=self.url_var, width=60)
        self.url_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        ttk.Label(input_frame, text="Format:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.format_combo = ttk.Combobox(input_frame, textvariable=self.format_var,
                                         values=["MP4 Video", "MP3 Audio", "WAV Audio"],
                                         state="readonly", width=15)
        self.format_combo.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        # --- Output Section ---
        output_frame = ttk.LabelFrame(main_frame, text="Output", padding="10")
        output_frame.pack(fill=tk.X, pady=(0, 10))
        output_frame.columnconfigure(1, weight=1) # Make label expand

        ttk.Label(output_frame, text="Save In:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.output_dir_label = ttk.Label(output_frame, textvariable=self.output_dir_var, relief="sunken", padding=2, background="white", anchor=tk.W)
        self.output_dir_label.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        self.browse_button = ttk.Button(output_frame, text="Select Folder...", command=self.browse_output_directory)
        self.browse_button.grid(row=0, column=2, padx=5, pady=5)

        # --- Action Buttons Frame ---
        # Create a frame to hold the Paste and Download buttons side-by-side
        action_button_frame = ttk.Frame(main_frame)
        action_button_frame.pack(pady=10) # Add padding around the frame

        # --- Paste Button ---
        self.paste_button = ttk.Button(action_button_frame, text="Paste URL from Clipboard", command=self.paste_from_clipboard)
        self.paste_button.pack(side=tk.LEFT, padx=(0, 5)) # Pack left, add padding to the right

        # --- Download Button ---
        self.download_button = ttk.Button(action_button_frame, text="Download", command=self.start_download)
        self.download_button.pack(side=tk.LEFT, padx=(5, 0)) # Pack left (next to paste), add padding to the left


        # --- Log Section ---
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding="10")
        log_frame.pack(expand=True, fill=tk.BOTH, pady=(0, 5))

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=10, state=tk.DISABLED, relief="sunken", borderwidth=1, font=("Consolas", 9)) # Use a monospaced font for log
        self.log_text.pack(expand=True, fill=tk.BOTH)

        # --- Initial Dependency Check ---
        if not self.yt_dlp_executable:
            self.log_message("ERROR: yt-dlp not found. Please ensure yt-dlp.exe is in the script's directory or your system PATH.", "error")
            self.download_button.config(state=tk.DISABLED)
            self.paste_button.config(state=tk.DISABLED) # Also disable paste if yt-dlp isn't found
        else:
             self.log_message(f"Found yt-dlp: {self.yt_dlp_executable}")

        if not self.ffmpeg_executable:
             self.log_message("WARNING: ffmpeg not found. Audio extraction/conversion (MP3/WAV) might fail. Ensure ffmpeg.exe is available.", "warning")
        elif self.ffmpeg_executable:
             self.log_message(f"Found ffmpeg: {self.ffmpeg_executable}")

        self.log_message(f"Default save directory: {self.output_dir_var.get()}")


    def log_message(self, message, level="info"):
        """Appends a message to the log window."""
        self.root.after(0, self._log_message_thread_safe, message, level)

    def _log_message_thread_safe(self, message, level):
        """Internal method to safely update log from threads."""
        try:
            self.log_text.config(state=tk.NORMAL)
            tag = ()
            prefix = ""
            if level == "error":
                tag = ("error",)
                prefix = "ERROR: "
            elif level == "warning":
                tag = ("warning",)
                prefix = "WARNING: "

            self.log_text.insert(tk.END, f"{prefix}{message}\n", tag)
            if not hasattr(self, "_tags_configured"):
                 self.log_text.tag_config("error", foreground="red")
                 self.log_text.tag_config("warning", foreground="dark orange")
                 self._tags_configured = True

            self.log_text.see(tk.END) # Scroll to the end
            self.log_text.config(state=tk.DISABLED)
        except tk.TclError as e:
             print(f"Tkinter error logging message: {e}")

    def paste_from_clipboard(self):
        """Gets text from the clipboard and pastes it into the URL entry."""
        try:
            clipboard_content = self.root.clipboard_get()
            self.url_var.set(clipboard_content)
            self.log_message("Pasted URL from clipboard.")
        except tk.TclError:
            # This error occurs if the clipboard is empty or doesn't contain text
            messagebox.showwarning("Paste Error", "Could not get text from clipboard. Is it empty?")
            self.log_message("Failed to paste from clipboard (empty or non-text?).", "warning")
        except Exception as e:
            messagebox.showerror("Paste Error", f"An unexpected error occurred while pasting: {e}")
            self.log_message(f"Unexpected error pasting from clipboard: {e}", "error")


    def browse_output_directory(self):
        """Opens the 'Select Folder' dialog."""
        directory = filedialog.askdirectory(
            initialdir=self.output_dir_var.get(),
            title="Select Download Folder"
        )
        if directory:
            self.output_dir_var.set(directory)
            self.log_message(f"Output directory set to: {directory}")


    def start_download(self):
        """Validates input and starts the download thread."""
        if self.is_downloading:
            self.log_message("Download already in progress.", "warning")
            return

        url = self.url_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        selected_format = self.format_var.get()

        if not self.yt_dlp_executable:
             self.log_message("Cannot download: yt-dlp executable not found.", "error")
             return

        if not url:
            self.log_message("Please enter or paste a URL.", "error") # Updated message
            return

        if not output_dir:
            self.log_message("Please select an output directory.", "error")
            return

        if not os.path.isdir(output_dir):
             self.log_message(f"Output directory does not exist: {output_dir}", "error")
             return

        if "Audio" in selected_format and not self.ffmpeg_executable:
            self.log_message("ffmpeg not found, required for audio extraction. Download might fail.", "warning")

        self.is_downloading = True
        self.download_button.config(state=tk.DISABLED)
        self.paste_button.config(state=tk.DISABLED) # Disable paste during download too
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.log_message(f"Starting download for: {url}")
        self.log_message(f"Format: {selected_format}")
        self.log_message(f"Saving in directory: {output_dir}")
        self.log_message("Filename will be generated from video title.")

        download_thread = threading.Thread(target=self.download_thread_target,
                                           args=(url, selected_format, output_dir),
                                           daemon=True)
        download_thread.start()

    def download_thread_target(self, url, selected_format, output_dir):
        """The actual download logic executed in a separate thread."""
        try:
            command = [self.yt_dlp_executable]

            # Add format options
            if selected_format == "MP4 Video":
                command.extend(['-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'])
                command.extend(['--merge-output-format', 'mp4'])
            elif selected_format == "MP3 Audio":
                command.extend(['-x', '--audio-format', 'mp3', '-f', 'bestaudio/best', '--audio-quality', '0'])
            elif selected_format == "WAV Audio":
                command.extend(['-x', '--audio-format', 'wav', '-f', 'bestaudio/best'])

            # Output Path using Template
            output_template = os.path.join(output_dir, "%(title)s.%(ext)s")
            command.extend(['-o', output_template])

            # Add ffmpeg location if found and needed
            if self.ffmpeg_executable and self.ffmpeg_executable != "ffmpeg":
                 ffmpeg_dir = os.path.dirname(self.ffmpeg_executable)
                 if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
                     command.extend(['--ffmpeg-location', ffmpeg_dir])
                 else:
                     if find_executable("ffmpeg", FFMPEG_PATH) == "ffmpeg":
                          self.log_message("Using ffmpeg found in PATH.", "info")
                     else:
                          self.log_message("Specific ffmpeg path seems invalid, may cause issues.", "warning")
            elif "Audio" in selected_format or selected_format == "MP4 Video":
                 if find_executable("ffmpeg", FFMPEG_PATH) == "ffmpeg":
                     self.log_message("Using ffmpeg found in PATH.", "info")

            # Add the URL last
            command.append(url)

            quoted_command = []
            for arg in command:
                if ' ' in arg and not (arg.startswith('"') and arg.endswith('"')):
                    quoted_command.append(f'"{arg}"')
                else:
                    quoted_command.append(arg)
            self.log_message(f"Executing command: {' '.join(quoted_command)}")

            # Start the process
            process = subprocess.Popen(command,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       text=True,
                                       encoding='utf-8',
                                       errors='replace',
                                       startupinfo=get_startup_info(),
                                       creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                                       )

            # Real-time Log Reading
            stdout_lines = []
            stderr_lines = []

            def reader_thread(pipe, storage):
                try:
                    if pipe:
                        for line in iter(pipe.readline, ''):
                            storage.append(line)
                            self.log_message(line.strip()) # Log immediately
                    else:
                        self.log_message("Warning: Standard output/error pipe is None.", "warning")
                except Exception as e:
                    self.log_message(f"Error reading pipe: {e}", "error")
                finally:
                    if pipe:
                        pipe.close()

            stdout_thread = threading.Thread(target=reader_thread, args=(process.stdout, stdout_lines))
            stderr_thread = threading.Thread(target=reader_thread, args=(process.stderr, stderr_lines))
            stdout_thread.start()
            stderr_thread.start()

            stdout_thread.join()
            stderr_thread.join()

            process.wait()

            # Final status check
            full_stderr = "".join(stderr_lines)
            if process.returncode != 0 or "error" in full_stderr.lower():
                 for line in stderr_lines:
                     if "error" in line.lower():
                         self.log_message(f"Error Detail: {line.strip()}", "error")
                 self.log_message(f"Download failed with exit code {process.returncode}.", "error")
            else:
                self.log_message("Download completed successfully!", "info")
                self.log_message(f"File saved in '{output_dir}' with name based on title.")

        except FileNotFoundError:
            self.log_message(f"ERROR: Command '{command[0]}' not found. Check yt-dlp/ffmpeg paths.", "error")
        except Exception as e:
            self.log_message(f"An unexpected error occurred during download setup or execution: {e}", "error")
            import traceback
            self.log_message(f"Traceback: {traceback.format_exc()}", "error")
        finally:
            self.root.after(0, self.enable_action_buttons) # Use new function to enable both buttons
            self.is_downloading = False

    def enable_action_buttons(self):
        """Safely re-enables the action buttons from the main thread."""
        try:
            # Only re-enable if yt-dlp was found initially
            if self.yt_dlp_executable:
                self.download_button.config(state=tk.NORMAL)
                self.paste_button.config(state=tk.NORMAL)
        except tk.TclError as e:
             print(f"Tkinter error enabling buttons: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = YtdlpGui(root)
    # Optional icon setting code remains commented out
    # try:
    #     if platform.system() == "Windows":
    #         root.iconbitmap('icon.ico')
    # except tk.TclError:
    #      print("Warning: Icon file not found or invalid.")
    # except Exception as e:
    #      print(f"Warning: Could not set icon - {e}")

    root.mainloop()

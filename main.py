import argparse
import asyncio
import io
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import threading
import time

import aiohttp.web_runner
import inotify.adapters
import inotify.constants
import psutil
from aiohttp import web

# Command line arguments variables
FFMPEG_NICE: int = 15
FFMPEG_CMD: str = "ffmpeg"
FFPROBE_CMD: str = "ffprobe"
INP_DIR: str = '/ffauto/input'
OUT_DIR: str = '/ffauto/output'
TMP_DIR: str = '/ffauto/temp'
ARCHIVE_DIR: str = '/ffauto/archive'
DEL_ORIG: bool = False
MOVE_ORIG: bool = False
OVERWRITE: bool = False
EXTENSIONS: list[str] = ['mp4', 'mkv', 'mpg', 'mpe', 'mov', 'avi', 'dv', 'ogv']
CODECS: list[str] = ["-map_chapters", "0", "-map_metadata", "0", "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx265",
                     "-crf", "28", "-b:v", "0", "-c:a", "aac", "-vbr", "4", "-c:s", "copy"]
OUT_EXT: str = "auto"
PORT: int = 8088
HOST: str = '0.0.0.0'
DEBUG: bool = True

# statistics variables
stat_file: str = ""
stat_progress: int = 0
stat_filesize: str = "0kB"
stat_avg_fps: float = 0
stat_avg_bitrate: str = '0kb/s'
stat_time_remaining: int = 0
stat_time_started: int = 0
stat_time_elapsed: int = 0
stat_cpu_util: int = 0

# thread/runtime variables
ws_thread: asyncio.AbstractEventLoop = None
cv_thread: threading.Thread = None
ffmp_proc: subprocess.Popen = None
enq_threads: list[threading.Thread] = []
SIGNAL_STOP: bool = False
conv_queue: list[tuple[str, str, str]] = []

# static variables
COLORS: tuple[str, str, str, str, str, str] = ("\33[31m", "\33[32m", "\33[33m", "\33[34m", "\33[35m", "\33[36m")


def d(component: str, message: str, newline=True, header=True) -> None:
    """
    Print debug message (if debug enabled)
    :param component: sender component
    :param message: message to print
    :param newline: whether to include newline at end of message
    :param header: whether to print the component name before message
    """
    if DEBUG:
        if header:
            print(f"{COLORS[ord(component[0]) % 6]}[{component.ljust(8)}]\33[0m [D] {message}",
                  end='\n' if newline else '', flush=True)
        else:
            print(message, end='\n' if newline else '', flush=True)


def i(component: str, message: str, newline=True, header=True) -> None:
    """
    Print info message
    :param component: sender component
    :param message: message to print
    :param newline: whether to include newline at end of message
    :param header: whether to print the component name before message
    """
    if header:
        print(f"{COLORS[ord(component[0]) % 6]}[{component.ljust(8)}]\33[0m \33[34m[I]\33[0m {message}",
              end='\n' if newline else '', flush=True)
    else:
        print(message, end='\n' if newline else '', flush=True)


def w(component: str, message: str, newline=True, header=True) -> None:
    """
    Print warning message
    :param component: sender component
    :param message: message to print
    :param newline: whether to include newline at end of message
    :param header: whether to print the component name before message
    """
    if header:
        print(f"{COLORS[ord(component[0]) % 6]}[{component.ljust(8)}]\33[0m \33[31m[W]\33[0m {message}",
              end='\n' if newline else '', flush=True)
    else:
        print(message, end='\n' if newline else '', flush=True)


###############################################################
# Webserver creation functions                                #
###############################################################

def start_webserver_background() -> None:
    """
    Starts the aiohttp server in a background thread
    :return: void
    """
    if PORT == 0:
        d("WS_INIT", "Webserver is disabled")
        return
    d("WS_INIT", "Starting webserver in background...")
    threading.Thread(target=start_webserver, args=(create_webserver(),)).start()


def create_webserver() -> aiohttp.web_runner.AppRunner:
    """
    Creates the aiohttp server AppRunner for starting in background thread
    :return: the AppRunner()
    """
    app = web.Application()
    app.add_routes([web.get('/', index), web.get('/index', index), web.get('/index.html', index),
                    web.get('/stats', web_stats),
                    web.get('/queue', web_queue),
                    web.get('/stop', web_stop),
                    web.get('/prio:{id}', web_prio),
                    web.get('/del:{id}', web_del),
                    web.get('/favicon.ico', favicon)])
    return web.AppRunner(app)


def start_webserver(runner: aiohttp.web_runner.AppRunner) -> None:
    """
    Starts the given webserver (AppRunner) in ws_thread Thread
    :param runner: Webserver (AppRunner) to start
    """
    global ws_thread
    assert ws_thread is None, "ws_thread must be None, cannot recreate webserver!"
    i("START_WS", "Start Webserver!")
    ws_thread = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_thread)
    ws_thread.run_until_complete(runner.setup())
    site = web.TCPSite(runner, HOST, PORT)
    ws_thread.run_until_complete(site.start())
    ws_thread.run_forever()
    i("START_WS", "Webserver stopped!")


###############################################################
# Webserver handler functions                                 #
###############################################################


async def web_prio(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    Moves the specified id to the top of the queue
    :param request: request to process
    :return: Response ["OK"
                       or Error 400 (no id specified)
                       or Error 404 (id not found in queue)]
    """
    global conv_queue
    target = request.match_info.get('id', None)
    if target is None:
        raise web.HTTPBadRequest()
    tgt_items = [item for item in conv_queue if item[2] == target]
    if len(tgt_items) == 0:
        raise web.HTTPNotFound()
    conv_queue.insert(0, conv_queue.pop(conv_queue.index(tgt_items[0])))
    return web.Response(text="OK")


async def web_del(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    Removes the given id from the queue
    :param request: request to process
    :return: Response ["OK"
                       or Error 400 (no id specified)
                       or Error 404 (id not found in queue)]
    """
    global conv_queue
    target = request.match_info.get('id', None)
    if target is None:
        raise web.HTTPBadRequest()
    tgt_items = [item for item in conv_queue if item[2] == target]
    if len(tgt_items) == 0:
        raise web.HTTPNotFound()
    conv_queue.remove(tgt_items[0])
    return web.Response(text="OK")


async def web_stop() -> aiohttp.web.Response:
    """
    Stops the current conversion
    :return:
    """
    global ffmp_proc
    if ffmp_proc is not None:
        ffmp_proc.terminate()
    return web.Response(text="")


async def web_queue() -> aiohttp.web.Response:
    """
    Returns the current queue, json-encoded
    :return:
    """
    return web.Response(text=json.dumps(conv_queue))


async def web_stats() -> aiohttp.web.Response:
    """
    Returns the current statistics, json-encoded
    :return:
    """
    return web.Response(text=json.dumps({
        "file": stat_file,
        "pct": stat_progress,
        "size": stat_filesize,
        "fps": stat_avg_fps,
        "rate": stat_avg_bitrate,
        "rem": stat_time_remaining,
        "ela": stat_time_elapsed,
        "sta": stat_time_started,
        "cpu": stat_cpu_util
    }))


async def favicon() -> aiohttp.web.FileResponse:
    """
    :return: the favicon
    """
    return web.FileResponse('./favicon.ico')


async def index() -> aiohttp.web.FileResponse:
    """
    :return: the index.html page
    """
    return web.FileResponse('./index.html')


# https://stackoverflow.com/a/56398787
def random_name() -> str:
    """
    Returns a random 8-character string
    TODO: also use uppercase letters?
    :return: random string
    """
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


# https://gist.github.com/leepro/9694638
def bytes2human(n: int, fmt: str = '%(value).1f %(symbol)s') -> str:
    """
    Converts the given integer to SI-prefixed value (k=1000, …)
    :param n: integer to convert
    :param fmt: format to use
    :return: formatted string
    """
    n = int(n)
    if n < 0:
        raise ValueError("n < 0")
    symbols = ('', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = {}
    for iv, s in enumerate(symbols[1:]):
        prefix[s] = 1 << (iv + 1) * 10
    for symbol in reversed(symbols[1:]):
        if n >= prefix[symbol]:
            value = float(n) / prefix[symbol]
            return fmt % locals()
    return fmt % dict(symbol=symbols[0], value=n)


def enqueue_file(filepath: str, delay: int = 5) -> None:
    """
    Waits until file is written completely (size not changed for $delay seconds),
    generates a random identifier and adds it to the conv_queue.
    :param filepath: file to add
    :param delay: seconds to wait filesize not to change
    """
    last_size = -1
    while last_size != os.path.getsize(filepath) and not SIGNAL_STOP:
        last_size = os.path.getsize(filepath)
        time.sleep(delay)
    if not SIGNAL_STOP:
        conv_queue.append((filepath, bytes2human(last_size), random_name()))
        i("ENQUEUE", f"Enqueued {filepath}")
    else:
        d("ENQUEUE", f"Stopped before enqueueing {filepath}")


def post_convert(input_file: str, temp_file: str, output_file: str, copy_temp: bool = True) -> None:
    """
    Takes care of post-conversion steps:
    - moves temp_file to output_file IF copy_temp enabled
    - moves input_file to archive IF MOVE_ORIG enabled - OR -
    - deletes input_file IF DEL_ORIG enabled
    :param input_file: input file
    :param temp_file: temp file
    :param output_file: output file
    :param copy_temp: whether to copy temp_file to output_file
    :return: nothing
    """
    if copy_temp:
        if os.path.exists(temp_file):
            shutil.move(temp_file, output_file)
            d("POSTPROC", f"Moved temp file to {output_file}")
        else:
            w("POSTPROC", f"Temp-File \"{temp_file}\" not found!")
            return
    archive_filename = os.path.join(ARCHIVE_DIR, os.path.basename(input_file))
    if MOVE_ORIG and (not os.path.exists(archive_filename) or OVERWRITE):
        if os.path.exists(input_file):
            shutil.move(input_file, archive_filename)
            d("POSTPROC", f"Moved {input_file} to archive {archive_filename}")
        else:
            w("POSTPROC", f"Input file \"{input_file}\" not found!")
    if DEL_ORIG and not MOVE_ORIG:
        if os.path.exists(input_file):
            os.remove(input_file)
            d("POSTPROC", f"Deleted input file {input_file}")
        else:
            w("POSTPROC", f"Input file \"{input_file}\" not found!")


def convert_file(input_tuple: tuple[str, str, str]) -> None:
    """
    Converts the given file-tuple (filename, size, temp-id),
    then executes post_convert() for post-steps
    :param input_tuple: input file (filename, size, temp-id)
    :return: nothing
    """
    global stat_file, stat_progress, stat_filesize, stat_avg_fps, stat_avg_bitrate, stat_time_remaining, \
        stat_time_elapsed, stat_time_started, stat_cpu_util, ffmp_proc
    input_file: str = input_tuple[0]
    output_ext: str = os.path.splitext(input_file)[1] if OUT_EXT == "auto" else f".{OUT_EXT}"
    output_file: str = os.path.join(OUT_DIR, os.path.splitext(os.path.basename(input_file))[0] + output_ext)
    temp_file: str = os.path.join(TMP_DIR, input_tuple[2] + output_ext)
    if os.path.exists(output_file):
        if os.path.getsize(output_file) == 0 or OVERWRITE:
            i("CONVERT", f"Output file exists, removing (empty or overwrite)")
            os.remove(output_file)
        else:
            i("CONVERT", f"Skipping conversion of {input_file} (exists)")
            post_convert(input_file, '', output_file, False)
            return

    d("CONVERT", f"Starting conversion of {input_file} - counting frames")
    # Get frame count
    ffmp_proc = subprocess.Popen(
        [FFPROBE_CMD, '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
         'stream=nb_read_packets', '-of', 'csv=p=0', input_file],
        stdout=subprocess.PIPE)
    frame_count: int = int(re.sub("[^0-9]", "", ffmp_proc.communicate()[0].decode()))

    i("CONVERT", f"Start transcoding of {input_file} ({frame_count} frames total)")
    stat_time_started = round(time.time())
    stat_file = os.path.basename(input_file)

    # Convert file
    # ['nice', '-n', FFMPEG_NICE,
    #  FFMPEG_CMD, '-y', '-loglevel', 'quiet', '-hide_banner', '-stats', '-i', input_file,
    #  '-map_chapters', '0', '-map_metadata', '0', '-map', '0:v:0', '-map', '0:a:0',
    #  '-x265-params', 'log-level=error', '-c:v', 'libx265', '-crf', '28', '-b:v', '0',
    #  '-c:a', 'aac', '-vbr', '4', '-c:s', 'copy', '-movflags', '+faststart', temp_file]
    ffmp_proc = subprocess.Popen(['nice', '-n', str(FFMPEG_NICE),
                                  FFMPEG_CMD, '-y', '-loglevel', 'quiet', '-hide_banner', '-stats',
                                  '-i', input_file, '-x265-params', 'log-level=error']
                                 + CODECS +
                                 ['-movflags', '+faststart', temp_file],
                                 stderr=subprocess.PIPE)
    ffps: psutil.Process = psutil.Process(ffmp_proc.pid)

    d("CONVERT", f"executing: {subprocess.list2cmdline(ffmp_proc.args)}")

    d("CONVERT", f"ffmpeg pid seems {ffmp_proc.pid}")

    # Parse progress and set global Stat-Variables for webserver
    reader:io.TextIOWrapper = io.TextIOWrapper(ffmp_proc.stderr, newline='\r', encoding='utf8')
    sbuf: str = ""
    while ffmp_proc.poll() is None and not SIGNAL_STOP:
        sbuf += reader.read(1)
        if sbuf.endswith("\r"):
            # from https://gist.github.com/edwardstock/90b41d4d53af4c32853073865a319222
            if DEBUG and not sbuf.startswith("frame"):
                d("CONVERT", sbuf[:-1].replace("\r", ""))
            outp = re.search(
                'frame=\s*(?P<nframe>[0-9]+)\s+fps=\s*(?P<nfps>[0-9.]+)\s+.*size=\s*(?P<nsize>[0-9]+)' +
                '(?P<ssize>kB|mB|b)?\s*time=\s*(?P<sduration>[0-9\:.]+)\s*bitrate=\s*(?P<nbitrate>[0-9.]+)' +
                '(?P<sbitrate>bits\/s|mbits\/s|kbits\/s)?.*(dup=(?P<ndup>\d+)\s*)?(drop=(?P<ndrop>\d+)\s*)?' +
                'speed=\s*(?P<nspeed>[0-9.]+)x',
                sbuf[:-1])
            frames_done = int(outp.group('nframe'))
            stat_filesize = outp.group("nsize") + outp.group("ssize")
            stat_avg_fps = round((3 * stat_avg_fps + float(outp.group("nfps"))) / 4, 2)
            # STAT_AVG_BITRATE = round( (3*STAT_AVG_BITRATE+float(outp.group("nbitrate")))/4 ,1) #141.2kbits/s
            stat_avg_bitrate = f"{outp.group('nbitrate')}{outp.group('sbitrate').replace('its/s', 'ps')}"
            stat_progress = round((frames_done / frame_count) * 100)
            stat_time_elapsed = round(time.time() - stat_time_started)
            stat_cpu_util = round(ffps.cpu_percent(interval=0.0))
            if stat_avg_fps > 0:
                stat_time_remaining = round((stat_time_remaining + ((frame_count - frames_done) / stat_avg_fps)) / 2)
            sbuf = ""

    if SIGNAL_STOP or ffmp_proc.returncode != 0:
        i("CONVERT", "Conversion stopped, cleaning up...")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        resetVars()
        return

    d("CONVERT", f"Done transcoding {input_file} to {temp_file}")

    post_convert(input_file, temp_file, output_file)
    resetVars()

    i("CONVERT", f"Done processing {input_file} to {output_file}")


def watch_conversion_queue():
    global conv_queue
    d("CV_QUEUE", "Conversion queue in mainloop()")
    while not SIGNAL_STOP:
        if len(conv_queue) > 0:
            d("CV_QUEUE", f"Starting job, {len(conv_queue)} remaining")
            convert_file(conv_queue.pop(0))
    d("CV_QUEUE", "Conversion queue terminated.")


def check_folders():
    d("FW_INIT", "Checking binaries...")
    ffm_path = shutil.which(FFMPEG_CMD)
    ffp_path = shutil.which(FFPROBE_CMD)
    if ffm_path is None:
        w("FW_INIT", "Could not find ffmpeg! Possible causes:")
        w("FW_INIT", "- ffmpeg not installed/not in $PATH --> provide a custom path with \"--ffmpeg <path/to/ffmpeg>\"")
        w("FW_INIT", "- non-existing path specified")
        w("FW_INIT", "- ffmpeg binary not executable --> chmod +x <path/to/ffmpeg>")
        sys.exit(1)
    else:
        d("FW_INIT", f"found ffmpeg at \"{ffm_path}\"")
    if ffp_path is None:
        w("FW_INIT", "Could not find ffprobe! Possible causes:")
        w("FW_INIT", "- ffprobe not installed/not in $PATH --> provide a custom path with \"--ffprobe "
                     "<path/to/ffprobe>\"")
        w("FW_INIT", "- non-existing path specified")
        w("FW_INIT", "- ffprobe binary not executable --> chmod +x <path/to/ffprobe>")
        sys.exit(1)
    else:
        d("FW_INIT", f"found ffprobe at \"{ffp_path}\"")
    d("FW_INIT", "Checking folders...")
    if not os.path.exists(TMP_DIR):
        if not os.access(os.path.abspath(os.path.join(TMP_DIR, os.pardir)), os.W_OK):
            w("FW_INIT", "ERROR: Cannot create TMP_DIR, parent is not writeable!")
            sys.exit(1)
        os.makedirs(TMP_DIR)
    if not os.path.exists(OUT_DIR):
        if not os.access(os.path.abspath(os.path.join(OUT_DIR, os.pardir)), os.W_OK):
            w("FW_INIT", "ERROR: Cannot create OUT_DIR, parent is not writeable!")
            sys.exit(1)
        os.makedirs(OUT_DIR)
    if not os.path.exists(INP_DIR):
        if not os.access(os.path.abspath(os.path.join(INP_DIR, os.pardir)), os.W_OK):
            w("FW_INIT", "ERROR: Cannot create INP_DIR, parent is not writeable!")
            sys.exit(1)
        os.makedirs(INP_DIR)
    if MOVE_ORIG and not os.path.exists(ARCHIVE_DIR):
        if not os.access(os.path.abspath(os.path.join(ARCHIVE_DIR, os.pardir)), os.W_OK):
            w("FW_INIT", "ERROR: Cannot create ARCHIVE_DIR, parent is not writeable!")
            sys.exit(1)
        os.makedirs(ARCHIVE_DIR)

    if not os.access(TMP_DIR, os.W_OK):
        w("FW_INIT", "ERROR: TMP_DIR is not writeable!")
        sys.exit(1)
    else:
        d("FW_INIT", "TMP_DIR is writeable.")

    if not os.access(OUT_DIR, os.W_OK):
        w("FW_INIT", "ERROR: OUT_DIR is not writeable!")
        sys.exit(1)
    else:
        d("FW_INIT", "OUT_DIR is writeable.")

    if not os.access(INP_DIR, os.W_OK) and (DEL_ORIG or MOVE_ORIG):
        w("FW_INIT", "ERROR: INP_DIR is not writeable, and moving/deleting input files enabled!")
        sys.exit(1)
    else:
        d("FW_INIT", "INP_DIR is writeable.")

    if not os.access(ARCHIVE_DIR, os.W_OK) and MOVE_ORIG:
        w("FW_INIT", "ERROR: ARCHIVE_DIR is not writeable, and moving input files enabled!")
        sys.exit(1)
    else:
        d("FW_INIT", "ARCHIVE_DIR is writeable.")


def start_conversion_thread():
    d("CV_INIT", "Starting conversion queue...")
    global cv_thread
    cv_thread = threading.Thread(target=watch_conversion_queue)
    cv_thread.start()


def watch_directory():
    global conv_queue
    i = inotify.adapters.Inotify()

    i.add_watch(INP_DIR, mask=(inotify.constants.IN_CREATE | inotify.constants.IN_MOVED_TO))

    while not SIGNAL_STOP:
        for event in i.event_gen(yield_nones=False, timeout_s=1):
            (_, type_names, path, filename) = event

            if (filename.endswith("mp4")):
                i("FLDRWATCH", f"Found new file: {filename}")
                nt = threading.Thread(target=enqueue_file, args=(os.path.join(path, filename),))
                enq_threads.append(nt)
                nt.start()

            enq_thread: threading.Thread
            for enq_thread in enq_threads:
                if not enq_thread.is_alive:
                    enq_threads.remove(enq_thread)

            time.sleep(0.5)
    d("FLDRWATCH", "Watchloop stopped!")


def resetVars():
    global stat_file, stat_progress, stat_filesize, stat_avg_fps, stat_avg_bitrate, stat_time_remaining, stat_time_elapsed, stat_time_started, stat_cpu_util, ffmp_proc
    ffmp_proc = None
    stat_file = ""
    stat_progress = 0
    stat_filesize = "0kB"
    stat_avg_fps = 0
    stat_avg_bitrate = 0
    stat_time_remaining = 0
    stat_time_started = 0
    stat_time_elapsed = 0
    stat_cpu_util = 0


def initVars():
    global FFMPEG_NICE, FFMPEG_CMD, FFPROBE_CMD, INP_DIR, OUT_DIR, TMP_DIR, ARCHIVE_DIR, DEL_ORIG, MOVE_ORIG, OVERWRITE, CODECS, EXTENSIONS, PORT, HOST, OUT_EXT
    if 'FFA_ENV' in os.environ:
        FFMPEG_NICE = FFMPEG_NICE if os.getenv('FFMPEG_NICE') is None else int(os.getenv('FFMPEG_NICE'))
        FFMPEG_CMD = os.getenv('FFMPEG_CMD') or FFMPEG_CMD
        FFPROBE_CMD = os.getenv('FFPROBE_CMD') or FFPROBE_CMD
        INP_DIR = os.getenv('INP_DIR') or INP_DIR
        OUT_DIR = os.getenv('OUT_DIR') or OUT_DIR
        TMP_DIR = os.getenv('TMP_DIR') or TMP_DIR
        ARCHIVE_DIR = os.getenv('ARCHIVE_DIR') or ARCHIVE_DIR
        DEL_ORIG = (os.getenv('DEL_ORIG') or str(int(DEL_ORIG))) == '1'
        MOVE_ORIG = (os.getenv('MOVE_ORIG') or str(int(MOVE_ORIG))) == '1'
        OVERWRITE = (os.getenv('OVERWRITE') or str(int(OVERWRITE))) == '1'
        CODECS = CODECS if os.getenv('CODECS') is None else os.getenv('CODECS').split('')
        EXTENSIONS = EXTENSIONS if os.getenv('EXTENSIONS') is None else os.getenv('EXTENSIONS').split(',')
        OUT_EXT = os.getenv('OUT_FMT') or OUT_EXT
        PORT = int(os.getenv('PORT') or PORT)
        HOST = os.getenv('HOST') or HOST
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument('input_dir', help="Input folder")
        parser.add_argument('output_dir', help="Output folder")
        parser.add_argument('-t', '--temp', help="Set temporary folder", default=TMP_DIR)  # optional
        parser.add_argument('-a', '--archive', help="Archive input files to specified folder", default='')  # optional
        parser.add_argument('-d', '--delete', help="Delete input files after conversion",
                            action='store_true')  # optional
        parser.add_argument('-r', '--replace', help="Overwrite/replace existing files in archive and output folders",
                            action='store_true')  # optional
        parser.add_argument('-n', '--nice', help="Set ffmpeg nice value. Must be root for negative values.",
                            default=FFMPEG_NICE)  # optional
        parser.add_argument('-m', '--ffmpeg', help="Set custom ffmpeg binary", default=FFMPEG_CMD)  # optional
        parser.add_argument('-p', '--ffprobe', help="Set custom ffprobe binary", default=FFPROBE_CMD)  # optional
        parser.add_argument('-c', '--codec', help="Set custom audio/video codecs and parameters",
                            default='')  # optional
        parser.add_argument('-e', '--extensions', help="Set file extensions to convert (comma-separated)",
                            default='')
        parser.add_argument('-o', '--output-fmt', help="Set output container, \"auto\" -> same as input",
                            default=OUT_EXT)  # optional
        parser.add_argument('-P', '--port', help="Port for status webserver, 0 to disable", type=int,
                            default=PORT)  # optional
        parser.add_argument('-H', '--host', help="Host for status webserver", default=HOST)  # optional
        p = parser.parse_args()

        INP_DIR = p.input_dir
        OUT_DIR = p.output_dir
        TMP_DIR = p.temp
        ARCHIVE_DIR = p.archive
        MOVE_ORIG = len(p.archive) >= 1
        DEL_ORIG = p.delete
        OVERWRITE = p.replace
        FFMPEG_NICE = p.nice
        FFMPEG_CMD = p.ffmpeg
        FFPROBE_CMD = p.ffprobe
        CODECS = CODECS if p.codec == '' else str(p.codec).split(" ")
        EXTENSIONS = EXTENSIONS if p.extensions else str(p.extensions).split(',')
        OUT_EXT = p.output_fmt
        PORT = p.port
        HOST = p.host


def handle_quit(sig, frame):
    print("\33[31m Got SIGINT, terminating... \33[0m", flush=True)
    global SIGNAL_STOP
    SIGNAL_STOP = True
    if ffmp_proc is not None:
        print("\33[31m killing ffmpeg... \33[0m", flush=True)
        ffmp_proc.kill()
    if ws_thread is not None:
        print("\33[31m stopping webserver... \33[0m", flush=True)

        for tsk in asyncio.all_tasks(ws_thread):
            tsk.cancel()

        ws_thread.call_soon_threadsafe(ws_thread.stop)

        while ws_thread.is_running():
            time.sleep(0.5)
            print(".", end="", flush=True)

        ws_thread.run_until_complete(ws_thread.shutdown_asyncgens())

    print("\33[31mexiting...\33[0m", flush=True)
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_quit)
    initVars()
    resetVars()
    check_folders()
    start_webserver_background()
    start_conversion_thread()
    watch_directory()

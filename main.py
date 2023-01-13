import inotify.adapters, inotify.constants
from aiohttp import web
import asyncio
import time
import os
import threading
import subprocess
import re
import io
import json
import string
import random
import sys
import signal
import psutil
import argparse
import urllib.parse

FFMPEG_NICE = 15
FFMPEG_CMD = "ffmpeg"
FFPROBE_CMD = "ffprobe"
INP_DIR = "/home/fsch/Videos"
OUT_DIR = "/home/fsch/Videos/out" #TODO: Verify folders
TMP_DIR = "/tmp/ffauto" #TODO: Verify folders/mkdir
ARCHIVE_DIR = "/tmp/archive"
DEL_ORIG = False
MOVE_ORIG = False
OVERWRITE = False
EXTENSIONS = ['mp4']
CODECS = "-map_chapters 0 -map_metadata 0 -map 0:v:0 -map 0:a:0 -c:v libx265 -crf 28 -b:v 0 -c:a aac -vbr 4 -c:s copy"
OUT_EXT = "auto"
PORT = 8088
HOST = '0.0.0.0'

STAT_FILE = ""
STAT_PROGRESS = 0
STAT_FILESIZE = "0kB"
STAT_AVG_FPS = 0
STAT_AVG_BITRATE = 0
STAT_TIME_REMAINING = 0
STAT_TIME_STARTED = 0
STAT_TIME_ELAPSED = 0
STAT_CPU_UTIL = 0

WS_THREAD = None
CV_THREAD = None
FFMP_PROC = None
ENQ_THREADS = []

SIGNAL_STOP = False
WAIT_THREADS = True

CONV_QUEUE = []

DEBUG = True

COLORS = ["\33[31m", "\33[32m", "\33[33m", "\33[34m", "\33[35m", "\33[36m"]

def d(component: str, message: str, newline = True, header = True):
    if DEBUG:
        if header:
            print(f"{COLORS[ord(component[0]) % 6]} {component.ljust(8)} |\33[0m {message}", end='\n' if newline else '', flush=True)
        else:
            print(message, end='\n' if newline else '', flush=True)

def start_webserver_background():
    if PORT == 0:
        d("WS_INIT", "Webserver is disabled")
        return
    d("WS_INIT", "Starting webserver in background...")
    threading.Thread(target=start_webserver, args=(create_webserver(),)).start()

def create_webserver():
    app = web.Application()
    app.add_routes([web.get('/', index), web.get('/index', index), web.get('/index.html', index),
                    web.get('/stats', web_stats),
                    web.get('/queue', web_queue),
                    web.get('/stop', web_stop),
                    web.get('/prio:{id}', web_prio),
                    web.get('/del:{id}', web_del),
                    web.get('/favicon.ico', favicon)])
    return web.AppRunner(app)

def web_prio(request):
    global CONV_QUEUE
    target = request.match_info.get('id', None)
    if target is None:
        raise web.HTTPBadRequest()
    tgt_items = [item for item in CONV_QUEUE if item[2] == target]
    if len(tgt_items) == 0:
        raise web.HTTPNotFound()
    CONV_QUEUE.insert(0, CONV_QUEUE.pop(CONV_QUEUE.index(tgt_items[0])))
    return web.Response(text="OK")

def web_del(request):
    global CONV_QUEUE
    target = request.match_info.get('id', None)
    if target is None:
        raise web.HTTPBadRequest()
    tgt_items = [item for item in CONV_QUEUE if item[2] == target]
    if len(tgt_items) == 0:
        raise web.HTTPNotFound()
    CONV_QUEUE.remove(tgt_items[0])
    return web.Response(text="OK")

def web_stop(request):
    global FFMP_PROC
    if FFMP_PROC is not None:
        FFMP_PROC.terminate()
    return web.Response(text="")
    
def start_webserver(runner):
    global WS_THREAD
    d("START_WS", "Start Webserver!")
    WS_THREAD = asyncio.new_event_loop()
    asyncio.set_event_loop(WS_THREAD)
    WS_THREAD.run_until_complete(runner.setup())
    site = web.TCPSite(runner, HOST, PORT)
    WS_THREAD.run_until_complete(site.start())
    WS_THREAD.run_forever()
    d("START_WS", "Webserver stopped!")

async def favicon(request):
    return web.FileResponse('./favicon.ico')

async def index(request):
    return web.FileResponse('./index.html')

async def web_queue(request):
    return web.Response(text=json.dumps(CONV_QUEUE))

async def web_stats(request):
    return web.Response(text=json.dumps({
        "file": STAT_FILE,
        "pct": STAT_PROGRESS,
        "size": STAT_FILESIZE,
        "fps": STAT_AVG_FPS,
        "rate": STAT_AVG_BITRATE,
        "rem": STAT_TIME_REMAINING,
        "ela": STAT_TIME_ELAPSED,
        "sta": STAT_TIME_STARTED,
        "cpu": STAT_CPU_UTIL
    }))

#https://stackoverflow.com/a/56398787
def random_name():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

# https://gist.github.com/leepro/9694638
def bytes2human(n, format='%(value).1f %(symbol)s', symbols='customary'):
    n = int(n)
    if n < 0:
        raise ValueError("n < 0")
    symbols = ('', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = {}
    for i, s in enumerate(symbols[1:]):
        prefix[s] = 1 << (i+1)*10
    for symbol in reversed(symbols[1:]):
        if n >= prefix[symbol]:
            value = float(n) / prefix[symbol]
            return format % locals()
    return format % dict(symbol=symbols[0], value=n)

def enqueue_file(filepath):
    last_size = -1    
    while last_size != os.path.getsize(filepath) and not SIGNAL_STOP:
        last_size = os.path.getsize(filepath)
        time.sleep(2)
    if not SIGNAL_STOP:
        CONV_QUEUE.append((filepath, bytes2human(last_size), random_name()))
        d("ENQUEUE", f"Enqueued {filepath}")
    else:
        d("ENQUEUE", f"Stopped before enqueueing {filepath}")

def post_convert(input_file, temp_file, output_file, copy_temp = True):
    if copy_temp:
        os.replace(temp_file, output_file)
        d("POSTPROC", f"Moved temp file to {output_file}")
    archive_filename = os.path.join(ARCHIVE_DIR, os.path.basename(input_file))
    if MOVE_ORIG and (not os.path.exists(archive_filename) or OVERWRITE):
        os.replace(input_file, archive_filename)
        d("POSTPROC", f"Moved {input_file} to archive {archive_filename}")
    if DEL_ORIG and not MOVE_ORIG:
        os.remove(input_file)
        d("POSTPROC", f"Deleted input file {input_file}")
    


def convert_file(input_tuple):
    global STAT_FILE, STAT_PROGRESS, STAT_FILESIZE, STAT_AVG_FPS, STAT_AVG_BITRATE, STAT_TIME_REMAINING, STAT_TIME_ELAPSED, STAT_TIME_STARTED, STAT_CPU_UTIL, FFMP_PROC
    input_file = input_tuple[0]
    output_ext = os.path.splitext(input_file)[1] if OUT_EXT == "auto" else f".{OUT_EXT}"
    output_file = os.path.join(OUT_DIR, os.path.splitext(os.path.basename(input_file))[0] + output_ext)
    temp_file = os.path.join(TMP_DIR, input_tuple[2] + output_ext)
    if os.path.exists(output_file):
        if os.path.getsize(output_file) == 0 or OVERWRITE:
            d("CONVERT", f"Output file exists, removing (empty or overwrite)")
            os.remove(output_file)
        else:
            d("CONVERT", f"Skipping conversion of {input_file} (exists)")
            post_convert(input_file, None, output_file, False)
            return

    d("CONVERT", f"Starting conversion of {input_file} - counting frames")
    # Get frame count
    FFMP_PROC = subprocess.Popen([FFPROBE_CMD, '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 
                             'stream=nb_read_packets', '-of', 'csv=p=0', input_file], 
                             stdout=subprocess.PIPE)
    frame_count = int(re.sub("[^0-9]", "", FFMP_PROC.communicate()[0].decode()))

    d("CONVERT", f"Start transcoding of {input_file} ({frame_count} frames total)")
    STAT_TIME_STARTED = round(time.time())
    STAT_FILE = os.path.basename(input_file)

    # Convert file
    # ['nice', '-n', FFMPEG_NICE,
    #  FFMPEG_CMD, '-y', '-loglevel', 'quiet', '-hide_banner', '-stats', '-i', input_file, 
    #  '-map_chapters', '0', '-map_metadata', '0', '-map', '0:v:0', '-map', '0:a:0',
    #  '-x265-params', 'log-level=error', '-c:v', 'libx265', '-crf', '28', '-b:v', '0', 
    #  '-c:a', 'aac', '-vbr', '4', '-c:s', 'copy', '-movflags', '+faststart', temp_file]
    FFMP_PROC = subprocess.Popen(['nice', '-n', str(FFMPEG_NICE),
                             FFMPEG_CMD, '-y', '-loglevel', 'quiet', '-hide_banner', '-stats',
                             '-i', input_file, '-x265-params', 'log-level=error']
                             + CODECS.split(" ") + 
                             ['-movflags', '+faststart', temp_file],
                             stderr=subprocess.PIPE)
    ffps = psutil.Process(FFMP_PROC.pid)

    d("CONVERT", f"executing: {subprocess.list2cmdline(FFMP_PROC.args)}")

    d("CONVERT", f"ffmpeg pid seems {FFMP_PROC.pid}")
    
    # Parse progress and set global Stat-Variables for webserver
    reader = io.TextIOWrapper(FFMP_PROC.stderr, newline='\r', encoding='utf8')
    sbuf = ""
    while FFMP_PROC.poll() is None and not SIGNAL_STOP:
        sbuf += reader.read(1)
        if sbuf.endswith("\r"):
            #from https://gist.github.com/edwardstock/90b41d4d53af4c32853073865a319222
            if DEBUG and not sbuf.startswith("frame"):
                d("CONVERT", sbuf[:-1].replace("\r", ""))
            outp = re.search('frame=\s*(?P<nframe>[0-9]+)\s+fps=\s*(?P<nfps>[0-9\.]+)\s+.*size=\s*(?P<nsize>[0-9]+)(?P<ssize>kB|mB|b)?\s*time=\s*(?P<sduration>[0-9\:\.]+)\s*bitrate=\s*(?P<nbitrate>[0-9\.]+)(?P<sbitrate>bits\/s|mbits\/s|kbits\/s)?.*(dup=(?P<ndup>\d+)\s*)?(drop=(?P<ndrop>\d+)\s*)?speed=\s*(?P<nspeed>[0-9\.]+)x', sbuf[:-1])
            frames_done = int(outp.group('nframe'))
            STAT_FILESIZE = outp.group("nsize") + outp.group("ssize")
            STAT_AVG_FPS = round((3*STAT_AVG_FPS+float(outp.group("nfps")))/4, 2)
            #STAT_AVG_BITRATE = round( (3*STAT_AVG_BITRATE+float(outp.group("nbitrate")))/4 ,1) #141.2kbits/s
            STAT_AVG_BITRATE = f"{outp.group('nbitrate')}{outp.group('sbitrate').replace('its/s', 'ps')}"
            STAT_PROGRESS = round((frames_done/frame_count)*100)
            STAT_TIME_ELAPSED = round(time.time() - STAT_TIME_STARTED)
            STAT_CPU_UTIL = round(ffps.cpu_percent(interval=0.0))
            if STAT_AVG_FPS > 0:
                STAT_TIME_REMAINING = round((STAT_TIME_REMAINING+((frame_count-frames_done)/STAT_AVG_FPS))/2)
            sbuf = ""
    
    if SIGNAL_STOP or FFMP_PROC.returncode != 0:
        d("CONVERT", "Conversion stopped, cleaning up...")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        resetVars()
        return
    
    d("CONVERT", f"Done transcoding {input_file} to {temp_file}")

    post_convert(input_file, temp_file, output_file)
    resetVars()

def watch_conversion_queue():
    global CONV_QUEUE
    d("CV_QUEUE", "Conversion queue in mainloop()")
    while not SIGNAL_STOP:
        if len(CONV_QUEUE) > 0:
            d("CV_QUEUE", f"Starting job, {len(CONV_QUEUE)} remaining")
            convert_file(CONV_QUEUE.pop(0))
    d("CV_QUEUE", "Conversion queue terminated.")

def check_folders():
    d("FW_INIT", "Checking folders...")
    if not os.path.exists(TMP_DIR):
        if not os.access(os.path.abspath(os.path.join(TMP_DIR, os.pardir)), os.W_OK):
            print("ERROR: Cannot create TMP_DIR, parent is not writeable!", flush=True)
            sys.exit(1)
        os.makedirs(TMP_DIR)
    if not os.path.exists(OUT_DIR):
        if not os.access(os.path.abspath(os.path.join(OUT_DIR, os.pardir)), os.W_OK):
            print("ERROR: Cannot create OUT_DIR, parent is not writeable!", flush=True)
            sys.exit(1)
        os.makedirs(OUT_DIR)
    if not os.path.exists(INP_DIR):
        if not os.access(os.path.abspath(os.path.join(INP_DIR, os.pardir)), os.W_OK):
            print("ERROR: Cannot create INP_DIR, parent is not writeable!", flush=True)
            sys.exit(1)
        os.makedirs(INP_DIR)
    if not os.path.exists(ARCHIVE_DIR):
        if not os.access(os.path.abspath(os.path.join(ARCHIVE_DIR, os.pardir)), os.W_OK):
            print("ERROR: Cannot create ARCHIVE_DIR, parent is not writeable!", flush=True)
            sys.exit(1)
        os.makedirs(ARCHIVE_DIR)


    if not os.access(TMP_DIR, os.W_OK):
        print("ERROR: TMP_DIR is not writeable!", flush=True)
        sys.exit(1)
    else:
        d("FW_INIT", "TMP_DIR is writeable.")

    if not os.access(OUT_DIR, os.W_OK):
        print("ERROR: OUT_DIR is not writeable!", flush=True)
        sys.exit(1)
    else:
        d("FW_INIT", "OUT_DIR is writeable.")

    if not os.access(INP_DIR, os.W_OK) and (DEL_ORIG or MOVE_ORIG):
        print("ERROR: INP_DIR is not writeable, and moving/deleting input files enabled!", flush=True)
        sys.exit(1)
    else:
        d("FW_INIT", "INP_DIR is writeable.")

    if not os.access(ARCHIVE_DIR, os.W_OK) and MOVE_ORIG:
        print("ERROR: ARCHIVE_DIR is not writeable, and moving input files enabled!", flush=True)
        sys.exit(1)
    else:
        d("FW_INIT", "ARCHIVE_DIR is writeable.")

    
    

def start_conversion_thread():
    d("CV_INIT", "Starting conversion queue...")
    global CV_THREAD
    CV_THREAD = threading.Thread(target=watch_conversion_queue)
    CV_THREAD.start()

def watch_directory():
    global CONV_QUEUE
    i = inotify.adapters.Inotify()

    i.add_watch(INP_DIR, mask=(inotify.constants.IN_CREATE|inotify.constants.IN_MOVED_TO))

    while not SIGNAL_STOP:
        for event in i.event_gen(yield_nones=False, timeout_s=1):
            (_, type_names, path, filename) = event
            
            if(filename.endswith("mp4")):
                d("FLDRWATCH", f"Found new file: {filename}")
                nt = threading.Thread(target=enqueue_file, args=(os.path.join(path, filename),))
                ENQ_THREADS.append(nt)
                nt.start()
            
            enq_thread: threading.Thread
            for enq_thread in ENQ_THREADS:
                if not enq_thread.is_alive:
                    ENQ_THREADS.remove(enq_thread)
                
            time.sleep(0.5)
    d("FLDRWATCH", "Watchloop stopped!")

def resetVars():
    global STAT_FILE, STAT_PROGRESS, STAT_FILESIZE, STAT_AVG_FPS, STAT_AVG_BITRATE, STAT_TIME_REMAINING, STAT_TIME_ELAPSED, STAT_TIME_STARTED, STAT_CPU_UTIL, FFMP_PROC
    FFMP_PROC = None
    STAT_FILE = ""
    STAT_PROGRESS = 0
    STAT_FILESIZE = "0kB"
    STAT_AVG_FPS = 0
    STAT_AVG_BITRATE = 0
    STAT_TIME_REMAINING = 0
    STAT_TIME_STARTED = 0
    STAT_TIME_ELAPSED = 0
    STAT_CPU_UTIL = 0

def initVars():
    global FFMPEG_NICE, FFMPEG_CMD, FFPROBE_CMD, INP_DIR, OUT_DIR, TMP_DIR, ARCHIVE_DIR, DEL_ORIG, MOVE_ORIG, OVERWRITE, CODECS, EXTENSIONS, PORT, HOST, OUT_EXT
    if 'FFA_ENV' in os.environ:
        FFMPEG_NICE = int(os.getenv('FFMPEG_NICE') or 15)
        FFMPEG_CMD = os.getenv('FFMPEG_CMD') or 'ffmpeg'
        FFPROBE_CMD = os.getenv('FFPROBE_CMD') or 'ffprobe'
        INP_DIR = os.getenv('INP_DIR') or '/ffauto/input'
        OUT_DIR = os.getenv('OUT_DIR') or '/ffauto/output'
        TMP_DIR = os.getenv('TMP_DIR') or '/ffauto/temp'
        ARCHIVE_DIR = os.getenv('ARCHIVE_DIR') or '/ffauto/archive'
        DEL_ORIG = (os.getenv('DEL_ORIG') or '0') == '1'
        MOVE_ORIG = (os.getenv('MOVE_ORIG') or '0') == '1'
        OVERWRITE = (os.getenv('OVERWRITE') or '0') == '1'
        CODECS = os.getenv('CODECS') or '-map_chapters 0 -map_metadata 0 -map 0:v:0 -map 0:a:0 -c:v libx265 -crf 28 -b:v 0 -c:a aac -vbr 4 -c:s copy'
        EXTENSIONS = (os.getenv('EXTENSIONS') or 'mp4 mkv mpg mpe mov avi dv ogv').split(' ')
        OUT_EXT = os.getenv('OUT_FMT') or 'auto'
        PORT = int(os.getenv('PORT') or 8088)
        HOST = os.getenv('HOST') or '0.0.0.0'
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument('input-dir', help="Input folder")
        parser.add_argument('output-dir', help="Output folder")
        parser.add_argument('-t', '--temp', help="Set temporary folder", default="/tmp/ffauto") #optional
        parser.add_argument('-a', '--archive', help="Archive input files to specified folder", default='') #optional
        parser.add_argument('-d', '--delete', help="Delete input files after conversion", action='store_true') #optional
        parser.add_argument('-r', '--replace', help="Overwrite/replace existing files in archive and output folders", action='store_true') #optional
        parser.add_argument('-n', '--nice', help="Set ffmpeg nice value. Must be root for negative values.", default=0) #optional
        parser.add_argument('-m', '--ffmpeg', help="Set custom ffmpeg binary", default="ffmpeg") #optional
        parser.add_argument('-p', '--ffprobe', help="Set custom ffprobe binary", default="ffprobe") #optional
        parser.add_argument('-c', '--codec', help="Set custom audio/video codecs and parameters", default="-map_chapters 0 -map_metadata 0 -map 0:v:0 -map 0:a:0 -c:v libx265 -crf 28 -b:v 0 -c:a aac -vbr 4 -c:s copy") #optional 
        parser.add_argument('-e', '--extensions', help="Set file extensions to convert", default="mp4 mkv mpg mpe mov avi dv ogv")
        parser.add_argument('-o', '--output-fmt', help="Set output container, \"auto\" -> same as input", default="auto") #optional
        parser.add_argument('-P', '--port', help="Port for status webserver, 0 to disable", type=int, default=8088) #optional
        parser.add_argument('-H', '--host', help="Host for status webserver", default="0.0.0.0") #optional
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
        CODECS = p.codec
        EXTENSIONS = p.extensions
        OUT_EXT = p.output_fmt
        PORT = p.port
        HOST = p.host
    






def handle_quit(sig, frame):
    global WAIT_THREADS
    print("\33[31m Got SIGINT, terminating... \33[0m", flush=True)
    global SIGNAL_STOP
    SIGNAL_STOP = True
    if FFMP_PROC is not None:
        print("\33[31m killing ffmpeg... \33[0m", flush=True)
        FFMP_PROC.kill()
    if WS_THREAD is not None:
        print("\33[31m stopping webserver... \33[0m", flush=True)
        
        for tsk in asyncio.all_tasks(WS_THREAD):
            tsk.cancel()

        WS_THREAD.call_soon_threadsafe(WS_THREAD.stop)

        while WS_THREAD.is_running():
            time.sleep(0.5)
            print(".", end="", flush=True)

        WS_THREAD.run_until_complete(WS_THREAD.shutdown_asyncgens())

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

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
CODECS = "-c:v libx265 -crf 28 -b:v 0 -c:a aac -vbr 4 -c:s copy"

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
            print(f"{COLORS[ord(component[0]) % 6]} {component.ljust(12)} |\33[0m {message}", end='\n' if newline else '', flush=not newline)
        else:
            print(message, end='\n' if newline else '', flush=not newline)

def start_webserver_background():
    d("INIT_WS", "Starting webserver in background...")
    threading.Thread(target=start_webserver, args=(create_webserver(),)).start()

def create_webserver():
    app = web.Application()
    app.add_routes([web.get('/', index), web.get('/index', index), web.get('/index.html', index),
                    web.get('/stats', web_stats),
                    web.get('/queue', web_queue),
                web.get('/{name}', web_handle)])
    return web.AppRunner(app)
    
def start_webserver(runner):
    global WS_THREAD
    d("START_WS", "Start Webserver!")
    WS_THREAD = asyncio.new_event_loop()
    asyncio.set_event_loop(WS_THREAD)
    WS_THREAD.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', 8088)
    WS_THREAD.run_until_complete(site.start())
    WS_THREAD.run_forever()
    d("START_WS", "Webserver stopped!")

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
    }));

async def web_handle(request):
    name = request.match_info.get('name', "Anonymous")
    text = "Hello, " + name
    return web.Response(text=text)

#https://stackoverflow.com/a/56398787
def random_name():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def wait_write_complete(inpath, infile):
    filepath = os.path.join(inpath, infile)
    last_size = -1
    d("ENQUEUE", "Waiting for write completion.", False)
    while last_size != os.path.getsize(filepath) and not SIGNAL_STOP:
        last_size = os.path.getsize(filepath)
        time.sleep(2)
        d("", ".", False, False)
    d("", "done", True, False)

def enqueue_file(filepath):
    last_size = -1
    d("ENQUEUE", "Waiting for write completion.", False)
    while last_size != os.path.getsize(filepath) and not SIGNAL_STOP:
        last_size = os.path.getsize(filepath)
        time.sleep(2)
        d("", ".", False, False)
    if not SIGNAL_STOP:
        d("", "done", True, False)
        CONV_QUEUE.append(filepath)
    else:
        d("", "stopped", True, False)

def post_convert(input_file, temp_file, output_file, copy_temp = True):
    if copy_temp:
        os.replace(temp_file, output_file)
        d("PCONVERT", f"Moved temp file to {output_file}")
    archive_filename = os.path.join(ARCHIVE_DIR, os.path.basename(input_file))
    if MOVE_ORIG and (not os.path.exists(archive_filename) or OVERWRITE):
        os.replace(input_file, archive_filename)
        d("PCONVERT", f"Moved {input_file} to archive {archive_filename}")
    if DEL_ORIG and not MOVE_ORIG:
        os.remove(input_file)
        d("PCONVERT", f"Deleted input file {input_file}")
    


def convert_file(input_file):
    global STAT_FILE, STAT_PROGRESS, STAT_FILESIZE, STAT_AVG_FPS, STAT_AVG_BITRATE, STAT_TIME_REMAINING, STAT_TIME_ELAPSED, STAT_TIME_STARTED, STAT_CPU_UTIL, FFMP_PROC
    output_file = os.path.join(OUT_DIR, os.path.basename(input_file))
    temp_file = os.path.join(TMP_DIR, random_name() + os.path.splitext(input_file)[1])
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
    FFMP_PROC = subprocess.Popen(['nice', '-n', FFMPEG_NICE,
                             FFMPEG_CMD, '-y', '-loglevel', 'quiet', '-hide_banner', '-stats', '-i', input_file, 
                             '-map_chapters', '0', '-map_metadata', '0', '-map', '0:v:0', '-map', '0:a:0',
                             '-x265-params', 'log-level=error', CODECS, '-movflags', '+faststart', temp_file],
                             stderr=subprocess.PIPE)
    ffps = psutil.Process(FFMP_PROC.pid)

    d("CONVERT", f"ffmpeg pid seems {FFMP_PROC.pid}")
    
    # Parse progress and set global Stat-Variables for webserver
    reader = io.TextIOWrapper(FFMP_PROC.stderr, newline='\r', encoding='utf8')
    sbuf = ""
    while FFMP_PROC.poll() is None and not SIGNAL_STOP:
        sbuf += reader.read(1)
        if sbuf.endswith("\r"):
            #from https://gist.github.com/edwardstock/90b41d4d53af4c32853073865a319222
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
    
    if SIGNAL_STOP:
        return
    
    d("CONVERT", f"Done transcoding {input_file} to {temp_file}")

    post_convert(input_file, temp_file, output_file)

    #TODO: Copy temp to output
    if os.path.exists(temp_file):
        d("CONVERT", f"Deleting temp-file {temp_file}")
        os.remove(temp_file)

    resetVars()

def watch_conversion_queue():
    global CONV_QUEUE
    d("CONV_QUEUE", "Conversion queue in mainloop()")
    while not SIGNAL_STOP:
        if len(CONV_QUEUE) > 0:
            d("CONV_QUEUE", f"Starting job, {len(CONV_QUEUE)} remaining")
            convert_file(CONV_QUEUE.pop())
    d("CONV_QUEUE", "Conversion queue terminated.")

def check_folders():
    d("INIT_CF", "Checking folders...")
    if not os.access(TMP_DIR, os.W_OK):
        print("ERROR: TMP_DIR is not writeable!")
        sys.exit(1)
    else:
        d("INIT_CF", "TMP_DIR is writeable.")

    if not os.access(OUT_DIR, os.W_OK):
        print("ERROR: OUT_DIR is not writeable!")
        sys.exit(1)
    else:
        d("INIT_CF", "OUT_DIR is writeable.")

    if not os.access(INP_DIR, os.W_OK) and (DEL_ORIG or MOVE_ORIG):
        print("ERROR: INP_DIR is not writeable, and moving/deleting input files enabled!")
        sys.exit(1)
    else:
        d("INIT_CF", "INP_DIR is writeable.")

    if not os.access(ARCHIVE_DIR, os.W_OK) and MOVE_ORIG:
        print("ERROR: ARCHIVE_DIR is not writeable, and moving input files enabled!")
        sys.exit(1)
    else:
        d("INIT_CF", "ARCHIVE_DIR is writeable.")

    if not os.path.exists(TMP_DIR):
        os.makedirs(TMP_DIR)
    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)
    if not os.path.exists(INP_DIR):
        os.makedirs(INP_DIR)
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)
    

def start_conversion_thread():
    d("INIT_CV", "checking folders")
    if not os.path.exists(TMP_DIR):
        os.makedirs(TMP_DIR)
    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)
    d("INIT_CV", "Starting conversion queue...")
    global CV_THREAD
    WS_THREAD = threading.Thread(target=watch_conversion_queue)
    WS_THREAD.start()

def watch_directory():
    global CONV_QUEUE
    i = inotify.adapters.Inotify()

    i.add_watch(INP_DIR, mask=(inotify.constants.IN_CREATE|inotify.constants.IN_MOVED_TO))

    while not SIGNAL_STOP:
        for event in i.event_gen(yield_nones=False, timeout_s=3):
            (_, type_names, path, filename) = event

            print("PATH=[{}] FILENAME=[{}] EVENT_TYPES={}".format(
                path, filename, type_names))
            
            if(filename.endswith("mp4")):
                nt = threading.Thread(target=enqueue_file, args=(os.path.join(path, filename),))
                ENQ_THREADS.append(nt)
                nt.start()
                #wait_write_complete(path, filename)
                #convert_file(path, filename)
            
            enq_thread: threading.Thread
            for enq_thread in ENQ_THREADS:
                if not enq_thread.is_alive:
                    ENQ_THREADS.remove(enq_thread)
                
            time.sleep(0.5)
    d("WLOOP", "Watchloop stopped!")

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

def envOrDef(key, defval):
    return 
    return defval if key not in os.environ else os.environ[key]

def initVars():
    if 'IS_DOCKER' in os.environ:
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
        CODECS = os.getenv('CODECS') or '-c:v libx265 -crf 28 -b:v 0 -c:a aac -vbr 4 -c:s copy'
        EXTENSIONS = (os.getenv('EXTENSIONS') or 'mp4').split(' ')
        
    #----FFMPEG_NICE = 15
    #FFMPEG_CMD = "ffmpeg"
    #FFPROBE_CMD = "ffprobe"
    #----INP_DIR = "/home/fsch/Videos"
    #----OUT_DIR = "/home/fsch/Videos/out" #TODO: Verify folders
    #----TMP_DIR = "/tmp/ffauto" #TODO: Verify folders/mkdir
    #----ARCHIVE_DIR = "/tmp/archive"
    #----DEL_ORIG = False
    #----MOVE_ORIG = False
    #----OVERWRITE = False
    #----CODECS=""
    #----EXTENSIONS=[]
    parser = argparse.ArgumentParser()
    parser.add_argument('inp', help="Input folder")
    parser.add_argument('outp', help="Output folder")
    parser.add_argument('-a', '--archive', help="Archive input files to specified folder", default=None)
    parser.add_argument('-d', '--delete', help="Delete input files after conversion", action='store_true')
    parser.add_argument('-r', '--replace', help="Overwrite/replace existing files in archive and output folders", action='store_true')
    parser.add_argument('-n', '--nice', help="Set ffmpeg nice value. Must be root for negative values.", default=0)
    parser.add_argument('-t', '--temp', help="Set temporary folder", default="/tmp/ffauto")
    parser.add_argument('-m', '--ffmpeg', help="Set custom ffmpeg binary", default="ffmpeg")
    parser.add_argument('-p', '--ffprobe', help="Set custom ffprobe binary", default="ffprobe")
    parser.add_argument('-c', '--codec', help="Set custom audio/video codecs and parameters", default="")
    parser.add_argument('-e', '--extensions', help="Set file extensions to convert")
    parser.parse_args()






def handle_quit(sig, frame):
    global WAIT_THREADS
    print("\33[31m Got SIGINT, terminating... \33[0m")
    global SIGNAL_STOP
    SIGNAL_STOP = True
    if FFMP_PROC is not None:
        print("\33[31m killing ffmpeg... \33[0m")
        FFMP_PROC.kill()
    if WS_THREAD is not None:
        print("\33[31m stopping webserver... \33[0m")
        WS_THREAD.call_soon_threadsafe(WS_THREAD.stop)

    if WAIT_THREADS:
        WAIT_THREADS = False
        print("\33[31m Waiting for threads to terminate: ", end='', flush=True)
        for thread in threading.enumerate():
            if thread.name != "MainThread":
                print(f"{thread.name}…", end='', flush=True)
                thread.join()

    print("\33[31mexiting...\33[0m")
    sys.exit(0)
    

if __name__ == '__main__':
    print(type(os.environ['IST_DOCKER']))
    sys.exit(1)
    signal.signal(signal.SIGINT, handle_quit)

    check_folders()

    resetVars()
    start_webserver_background()
    start_conversion_thread()
    watch_directory()

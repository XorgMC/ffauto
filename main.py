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

INP_DIR = "/home/fsch/Videos"
OUT_DIR = "/home/fsch/Videos/out" #TODO: Verify folders
TMP_DIR = "/tmp/ffauto" #TODO: Verify folders/mkdir

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
    app.add_routes([web.get('/', web_handle),
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

def convert_file(input_file):
    global STAT_FILE, STAT_PROGRESS, STAT_FILESIZE, STAT_AVG_FPS, STAT_AVG_BITRATE, STAT_TIME_REMAINING, STAT_TIME_ELAPSED, STAT_TIME_STARTED, STAT_CPU_UTIL, FFMP_PROC
    d("CONVERT", f"Starting conversion of {input_file} - counting frames")
    output_file = os.path.join(OUT_DIR, os.path.basename(input_file))
    temp_file = os.path.join(TMP_DIR, random_name() + os.path.splitext(input_file)[1])

    # Get frame count
    FFMP_PROC = subprocess.Popen(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 
                             'stream=nb_read_packets', '-of', 'csv=p=0', input_file], 
                             stdout=subprocess.PIPE)
    frame_count = int(re.sub("[^0-9]", "", FFMP_PROC.communicate()[0].decode()))

    d("CONVERT", f"Start transcoding of {input_file} ({frame_count} frames total)")
    STAT_TIME_STARTED = round(time.time())
    STAT_FILE = os.path.basename(input_file)

    # Convert file
    FFMP_PROC = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'quiet', '-hide_banner', '-stats', '-i', input_file, 
                             '-map_chapters', '0', '-map_metadata', '0', '-map', '0:v:0', '-map', '0:a:0',
                             '-c:v', 'libx265', '-x265-params', 'log-level=error', '-crf', '28', '-b:v', '0', 
                             '-c:a', 'aac', '-vbr', '4', '-c:s', 'copy', '-movflags', '+faststart', temp_file],
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
            STAT_AVG_BITRATE = round( (3*STAT_AVG_BITRATE+float(outp.group("nbitrate")))/4 ,1)
            STAT_PROGRESS = round((frames_done/frame_count)*100)
            STAT_TIME_ELAPSED = round(time.time() - STAT_TIME_STARTED)
            STAT_CPU_UTIL = round(ffps.cpu_percent(interval=0.0))
            if STAT_AVG_FPS > 0:
                STAT_TIME_REMAINING = round((STAT_TIME_REMAINING+((frame_count-frames_done)/STAT_AVG_FPS))/2)
            sbuf = ""
    
    if SIGNAL_STOP:
        return
    
    d("CONVERT", f"Done transcoding {input_file} to {temp_file}")

    #TODO: Copy temp to output
    if os.path.exists(temp_file):
        d("CONVERT", f"Deleting temp-file {temp_file}")
        os.remove(temp_file)

    initVars()

def watch_conversion_queue():
    global CONV_QUEUE
    d("CONV_QUEUE", "Conversion queue in mainloop()")
    while not SIGNAL_STOP:
        if len(CONV_QUEUE) > 0:
            d("CONV_QUEUE", f"Starting job, {len(CONV_QUEUE)} remaining")
            convert_file(CONV_QUEUE.pop())
    d("CONV_QUEUE", "Conversion queue terminated.")

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

def initVars():
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
    signal.signal(signal.SIGINT, handle_quit)
    initVars()
    start_webserver_background()
    start_conversion_thread()
    watch_directory()

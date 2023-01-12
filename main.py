import inotify.adapters, inotify.constants
import time
import os
import subprocess
import re

INP_DIR = "/home/fsch/Videos"
OUT_DIR = "/home/fsch/Videos/out" #TODO: Verify folders
TMP_DIR = "/tmp/ffauto" #TODO: Verify folders/mkdir

def wait_write_complete(inpath, infile):
    filepath = os.path.join(inpath, infile)
    last_size = -1
    print("Waiting for write completion.", end='')
    while(last_size != os.path.getsize(filepath)):
        last_size = os.path.getsize(filepath)
        time.sleep(2)
        print(".", end='')
    print()
        

def convert_file(inpath, infile):
    print("converting " + inpath + "/" + infile)
    infpath = os.path.join(inpath, infile)
    outpath = os.path.join(OUT_DIR, infile) #TODO: Encode to TMP_DIR!
    # Get frame count
    #ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 FILE
    ffpr = subprocess.Popen(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 
                             'stream=nb_read_packets', '-of', 'csv=p=0', infpath], 
                             stdout=subprocess.PIPE)
    frame_count = int(re.sub("[^0-9]", "", ffpr.communicate()[0].decode()))

    ffmp = subprocess.Popen(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'quiet', '-stats', '-i', infpath, 
                             '-map_chapters', '0', '-map_metadata', '0', '-map', '0:v:0', '-map', '0:a:0',
                             '-c:v', 'libx265', '-crf', '28', '-b:v', '0', '-c:a', 'libfdk_aac', '-vbr', '4',
                             '-c:s', 'copy', '-movflags', '+faststart', outpath],
                             stderr=subprocess.PIPE)
    while True:
        output = process.stderr.readline()
        if output == '' and process.poll() is not None:
            break
        if output:
            print "*" + output.strip() + "*"
    rc = process.poll()
    print(f"{infile} has {frame_count} frames")
     

def watch_directory():
    i = inotify.adapters.Inotify()

    i.add_watch(INP_DIR, mask=(inotify.constants.IN_CREATE|inotify.constants.IN_MOVED_TO))

    for event in i.event_gen(yield_nones=False):
        (_, type_names, path, filename) = event

        print("PATH=[{}] FILENAME=[{}] EVENT_TYPES={}".format(
              path, filename, type_names))
        
        if(filename.endswith("mp4")):
            wait_write_complete(path, filename)
            convert_file(path, filename)
            
        time.sleep(5)

if __name__ == '__main__':
    watch_directory()

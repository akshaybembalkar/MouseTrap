# This python application will take a flac audio file name, or all flac files in a folder, then use
# Google's Full Duplex Speech API to convert the speech to text.  The result from Google is in json format
# and this script outputs the log and results into a csv format

import os
import random
import json
import getopt
import glob
import csv
from threading import Thread
from struct import *
import time
import sys
import logging
from pygsr import Pygsr

import requests

'''
import logging
LOGGER = logging.getLogger(__name__)

import mousetrap.plugins.interface as interface
'''

'''
class voiceRecognitio(interface.Plugin):
    def __init__(self, config):
        self._config = config
        self._flac_reader = fLaC_Reader(config)

    def run(self, app):
        self._flac_reader.update(app.pointer)
'''


class fLaC_Reader(object):
    error = ""
    sampleRate = 0
    channels = 0
    bitsperSample = 0
    length = 0.0
    minBlockSize, maxBlockSize = 0, 0

    def __init__(self, file):
        bytes = file.read(4)  # get Magic Number
        if bytes != "fLaC":
            self.error += "Not a fLaC file! Aborting\n"
        bytes = file.read(1)  # Get STREAMINFO metadata Block Header
        if ord(bytes) == 0:  # "STREAMINFO BLOCK FOUND"
        # Jump to the STREAMINFO Block, 24 bits from here.
            file.read(3)
        # parse STREMINFO BLOCK
            self.minBlockSize, self.maxBlockSize = unpack('>HH', file.read(4))
            self.minFrameSize = unpack('>I', '\x00' + file.read(3))
            self.FrameSize = unpack('>I', '\x00' + file.read(3))
            if self.minBlockSize < 16 or self.maxBlockSize < 16:
                self.error += "Invalid Block Size! Aborting!\n"
                # if minBlockSize == maxBlockSize:
                # print "Fixed Blocksize: %d samples" % maxBlockSize
            sampleInfo = file.read(8)
            sampleInfoBytes = unpack('>Q', sampleInfo)[0]
            self.sampleRate = sampleInfoBytes >> 44
            self.channels = ((sampleInfoBytes >> 41) & 7) + 1
            self.bitsperSample = ((sampleInfoBytes >> 36) & 0x1F) + 1
            self.length = (sampleInfoBytes & 0x0000000FFFFFF) / float(self.sampleRate)

        else:
            self.error += "STREAMINFO BLOCK not first\n"
    
    
class GoogleSpeechAPI(object):
    result = ''
    length = 0
    sampleRate = 0

    def __init__(self, file):
        self.logger = logging.getLogger(__name__)
        self.result = []
        self.file = file
        self.flac = fLaC_Reader(file)
        self.response_lines = []
        self.length = self.flac.length
        self.sampleRate = self.flac.sampleRate
        self.upstream_url = "https://www.google.com/speech-api/full-duplex/v1/up?key=%(key)s&pair=%(pair)s&lang=en-US&client=chromium&continuous&interim&pFilter=0"
        self.upstream_headers = {'content-type': 'audio/x-flac; rate=' + str(self.flac.sampleRate)}
        self.downstream_url = "https://www.google.com/speech-api/full-duplex/v1/down?pair=%(pair)s"
        self.api_key = "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"
        self.timeSinceResponse = 0
        self.response = ""
        self.connectionSuccessful = False
        self.no_result = False

    def getPair(self):
        return hex(random.getrandbits(64))[2:-1]

    def start(self):
        if self.flac.error != "":
            self.result = self.flac.error
            return
        pair = self.getPair()
        upstream_url = self.upstream_url % {"pair": pair, "key": self.api_key}
        downstream_url = self.downstream_url % {"pair": pair, "key": self.api_key}
        self.file.seek(0)
        self.upsession = requests.Session()
        self.downsession = requests.Session()
        self.upstream_thread = Thread(target=self.upstream, args=(upstream_url,))
        self.downstream_thread = Thread(target=self.downstream, args=(downstream_url,))

        self.downstream_thread.start()
        self.upstream_thread.start()

        self.stop()

    def stop(self):
        self.downstream_thread.join()
        self.upstream_thread.join()

    def gen_data(self):
        while True:
            item = self.file.read((self.flac.minBlockSize * self.flac.bitsperSample) / 8)
            if item:
                self.logger.debug("%d bytes sent" % len(item))
                yield item
            else:
                if self.no_result or self.timeSinceResponse > 2:
                    # self.result.append(self.response)
                    return #Google is Done Responding, close UpStream
                time.sleep(.5)
                self.timeSinceResponse += .5
                yield "00000000"

    def final(self):
        try:
          response = json.loads(self.response)
          if response['result']:
              if 'final' in response['result'][0]:
                  return response['result'][0]['final']
        except Exception, e:
          # assuming invalid JSON, return False
          self.logger.warning("exception testing latest line for final: '%s'" % self.response)
        return False


    def upstream(self, url):
        result = self.upsession.post(url, headers=self.upstream_headers, data=self.gen_data())
        upstream_request_status = result.status_code
        upstream_request_content = result.content
        if upstream_request_status != 200:
            self.logger.warning("failed request, status code %d, info: %s" % (upstream_request_status,result.content))
            self.start()
            raise RuntimeException("upstream request exception")
        self.logger.info("request upstream content submission response is: %s" % upstream_request_content)

    def downstream(self, url):
        r = self.downsession.get(url, stream=True)
        self.status_code = r.status_code
        if r.status_code == 200:
            for line in r.iter_lines(): #response_content.splitlines():
                self.timeSinceResponse = 0
                self.response = line
                self.response_lines.append(line)
                if line == '{"result":[]}':
                    # Google sends back an empty result signifying a successful connection
                    if not self.connectionSuccessful:
                        self.connectionSuccessful = True
                    else: # another empty response means Google couldn't find anything in the audio ...
                        self.logger.info("No Recongnizable Dialogue, closing stream")
                        # Making pretty for result repacker
                        self.result.append('{"result":[{"alternative":[{"transcript":"","confidence":0.99999}],"final":true}],"result_index":0}')
                        self.no_result = True
                if self.final():
                    self.result.append(line)
                    self.response = ""
            self.logger.info("request downstream content response is: %s" % self.response_lines)
        else:
            self.logger.warning("Failed to connect downstream. Response is: %s \n %s" %(r.status_code, r.content))
            self.logger.info("Restarting Attempt")
            self.start()


class Timer:
    def __enter__(self):
        self.start = time.clock()
        return self

    def __exit__(self, *args):
        self.end = time.clock()
        self.interval = self.end - self.start


def start():
        speech = Pygsr()
    # duration in seconds
        speech.record(4)

        output = "log.csv"
        input = "audio.flac"
        if input[-5:] != '.flac':
            input += '*.flac'
        files = glob.glob(input)

        for flac in files:
            print "opening %s:" % flac
            valid_result = False
            tries = 0
            with open(flac, 'rb') as f:
                result = GoogleSpeechAPI(f)
                print "Audio is %.03f seconds long" % result.length
                f.seek(0)
                with Timer() as t:
                    result.start()
            print "Result took %.03f sec" % t.interval
            print result.result

            out = ''.join(result.result)
            split3 = ""
            try:
               split1 = [x.strip() for x in out.split(':')]
               split2 = split1[3]
               split3 = [x.strip() for x in split2.split(',')]
		#[1:-1] is to remove double quotes from string
               print split3[0][1:-1]
               return split3[0][1:-1]
            except Exception, e:
               print "Exception"


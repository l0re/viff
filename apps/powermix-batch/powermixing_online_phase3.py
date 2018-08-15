#!/usr/bin/env python



from optparse import OptionParser
import viff.reactor
viff.reactor.install()
from twisted.internet import reactor
from viff.active import ActiveRuntime
from viff.field import GF
from random import randint
import time
import math
from viff.runtime import create_runtime, gather_shares,Share,make_runtime_class
from viff.comparison import Toft05Runtime
from viff.config import load_config
from viff.util import rand, find_prime
from viff.active import BasicActiveRuntime, TriplesHyperinvertibleMatricesMixin

import sys

import os
import sys
sys.setrecursionlimit(2000000)
# We start by defining the protocol, it will be started at the bottom
# of the file.
start = 0



def record_start():
    global start
    start = time.time()
    print "*" * 64
    print "Started"


def record_stop():
    global start
    stop = time.time()
    print
    print "Total time used: %.3f sec" % (stop-start)
    start = stop
    '''
    if runtime.id == 1:
        f = open('time.txt', 'w')
        f.write(stop-start)
        f.close()
    '''
    print "*" * 64

    #return x


class OnlineProtocol:

	def __init__(self, runtime,k,b):
		self.k = k
		self.batch = b
		self.runtime = runtime
		self.inputs = [[0 for _ in range(self.k)] for _ in range(self.batch)]
		self.p = find_prime(2**256, blum=True)
		self.Zp = GF(self.p)
		self.open_value= [[0 for _ in range(self.k)] for _ in range(self.batch)]

		record_start()
		# load shares from file
		self.load_input_from_file(self.k,self.p,self.batch)
		print "load input finished"
		record_stop()
		for b in range(self.batch):
			for i in range(self.k):
				self.open_value[b][i] = self.runtime.open(self.inputs[b][i])
		#print self.open_value
		shares = []
		for i in self.open_value:
			shares = shares + i
		result = gather_shares(shares)
		result.addCallback(self.create_output,self.batch)
		#self.runtime.schedule_callback(results, lambda _: self.runtime.synchronize())
	        #self.runtime.schedule_callback(results, lambda _: self.runtime.shutdown())

	def plainprint(self,result):
		print "done!"


	def load_input_from_file(self,k,p,b):
		for batch in range(b):
			filename = "powers.sum" + str(self.runtime.id) + "_batch" + str(batch+1)
		
			FD = open(filename, "r")
			line = FD.readline()
			if int(line) != p:
				print "p dismatch!! p in file is %d"%(int(line))
			line = FD.readline()
			#if int(line) != k:
			#	print "k dismatch!! k in file is %d"%(int(line))


			line = FD.readline()
			i = 0
			while line and i < self.k:
				#print i
				self.inputs[batch][i] = Share(self.runtime,self.Zp,self.Zp(int(line)))

				line = FD.readline()  
				i = i + 1


	def create_output(self,result,batch):

		print "value open finished"
		record_stop()
		for b in range(batch):
			filename = "party" + str(self.runtime.id) + "-powermixing-online-phase3-output-batch" + str(b+1)

			FD = open(filename, "w")

			content =  str(self.p) + "\n" + str(self.k) + "\n"

			for share in self.open_value[b]:
				content = content + str(share.result)[1:-1] + "\n"
			FD.write(content)
			FD.close()
			print "file outputs finished"
			record_stop()
	   	results = self.runtime.synchronize()
	        self.runtime.schedule_callback(results, lambda _: self.runtime.shutdown())

def errorHandler(failure):
    print("Error: %s" % failure)
# Parse command line arguments.
parser = OptionParser()
Toft05Runtime.add_options(parser)
options, args = parser.parse_args()

if len(args) == 0:
    parser.error("you must specify a config file")
else:
    id, players = load_config(args[0])
k = int(sys.argv[3])
batch = int(sys.argv[4])
# Create a deferred Runtime and ask it to run our protocol when ready.
runtime_class = make_runtime_class(runtime_class=BasicActiveRuntime,
    mixins=[TriplesHyperinvertibleMatricesMixin])
pre_runtime = create_runtime(id, players, 1, options, runtime_class=runtime_class)
pre_runtime.addCallback(OnlineProtocol,k,batch)
pre_runtime.addErrback(errorHandler)

# Start the Twisted event loop.
reactor.run()

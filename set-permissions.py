#!/usr/bin/python3

# 
# Program:  Give Permissions Program
#
# Summary:...
#
# Input: ...
#
# Output: ...	
#

import subprocess
import sys
import os
import time
import getpass
import paramiko
import pypyodbc
import csv
import threading
import logging

VERBOSE_MODE =				True	# Verbose Mode: Currently only prints the commands to run
TEST_MODE = 				False	# Must be True or False. If True, then just print out some test commands from a test file
ASSETDB_QUERY_BRIEF = 		True	# If true, only query a specified sample size of switches found in the AssetDB

if (TEST_MODE == False):
	RUN_COMMANDS_CONFIG_FILE = "commands.cfg"
else:
	RUN_COMMANDS_CONFIG_FILE = "configtest.cfg"

if (ASSETDB_QUERY_BRIEF == False):
	# Selects ALL Production 3750/3850's currently in the AssetDB
	# Not all values in DNSname column are DNS names, searching for ".net.pitt.edu" fixes this issue
	ASSETDB_QUERY = "SELECT DNSname FROM Asset_Table WHERE Status LIKE \'Production\' AND " \
					"DNSname LIKE \'%.c3750.net.pitt.edu%\' OR DNSname LIKE \'%.c3850.net.pitt.edu%\'"  
else:
	# Query only a sample size of switches (for testing purposes)
	SWITCH_NUM = "5" 
	ASSETDB_QUERY = "SELECT TOP " + SWITCH_NUM + " DNSname FROM Asset_Table WHERE Status LIKE \'Production\' AND " \
					"DNSname LIKE \'%.c3750.net.pitt.edu%\' OR DNSname LIKE \'%.c3850.net.pitt.edu%\'"

logging.basicConfig(filename="main_program.log", level=logging.DEBUG,  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
#logging.basicConfig(filename="main_program.log", level=logging.DEBUG,  format='(%(threadName)-10s) %(message)s')     # old config, threads listed by number

# For Paramiko to automatically SSH to clients prompting for missing host keys
class AllowAllKeys(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        return

# Description: switchThread class implements a thread (like in Java, the class is a thread itself)
#              run() method overridden, runs when thread.start() is called in main program
#              All implemented functions are encapsulated in the class itself (mainly for readability)
#              Should put into separate class file, switchThread.py, if used in the future
# Procedure: (1) Initialize thread, (2) Log into switch, (3) Run commands, (4) Save/Log Terminal 
#             Input and Output, and (5) Return result to main program
class switchThread(object):
	def __init__(self, switchName, logFileName, username, password, commandTable, switchResultTable):
	
		self.lock = threading.Lock()
		self.switchName = switchName
		
		self.logger = logging.getLogger(switchName + "-Logger")
		self.logger.setLevel(logging.DEBUG);
		self.fileHandler = logging.FileHandler(logFileName);
		self.formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
		self.fileHandler.setFormatter(self.formatter);
		self.logger.addHandler(self.fileHandler)
		self.logger.debug("INIT function for: " + self.switchName);  #ccliff test
		
		self.username = username
		self.password = password
		self.commandTable = commandTable
		
		# override run method, start when switchThread starts 
		threadResult = self.run();
		self.logger.debug("Result: " + threadResult);
		
		# switchResultTable is passed to threads by reference, use it to update the main program about the success or 
		# failure of the paramiko SSH session.  Surprisingly, it's fine to pass dictionaries by reference to threads, 
		# resource locking is built in for this variable type in Python.  
		# @bee14 I tested with and without this variable, doesn't seem to slow down long thread runtime
		switchResultTable[switchName] = threadResult;  # result will be success or failure string, returned to main program
		
		self.logger.debug("Exiting thread now...")
		return None
		
	def run(self):
		self.logger.debug("Running Thread for " + self.switchName);
		
		# Thread Task #1: Log into the switch
		isSuccessful = self.switchLogin()
		if(isSuccessful == True):
			self.logger.debug("    Successfully logged in via SSH")
		else:
			self.logger.error("    Unable to login to " + self.switchName)
			return "SSH_LOGIN_FAILURE"
		
		# Thread Task #2: Run commands from commands.cfg file
		self.logger.debug(self.runCommands())
	
		# Thread Task #3: Output Switch Terminal Stdout (from SSH session) for logging
		self.logger.debug(self.retrieveTerminalOutput())
		
		return "LOGIN_SUCCESS_COMMANDS_RUN"
		
	def switchLogin(self):
		self.client = paramiko.SSHClient()
		self.client.load_system_host_keys()
		self.client.load_host_keys(os.path.expanduser('~/.ssh/known_hosts'))
		self.client.set_missing_host_key_policy(AllowAllKeys())
		try:
			self.client.connect(self.switchName, username=self.username, password=self.password, timeout=15)
		except Exception:
			self.logger.debug("\t    Paramiko SSH Connection Error, switch: " + self.switchName) 
			return False
		
		# get the stdin / stdout files from the paramiko client ssh session
		self.channel = self.client.invoke_shell()
		self.pstdin = self.channel.makefile('wb')
		self.pstdout = self.channel.makefile('rb')
		
		return True
	
	def runCommands(self):
		self.pstdin.write("term shell\n")   #enables some common commands, such as "echo"
		time.sleep(0.1)
		
		if(TEST_MODE == False):
			self.pstdin.write("configure terminal\n")   #enter configuration terminal
			time.sleep(0.1)

		for cmd in self.commandTable.keys():
			self.pstdin.write("%s\n" % self.commandTable[cmd])
			time.sleep(0.1);
	
		if(TEST_MODE == False):
			self.pstdin.write("end\n")   #exit configuration terminal
			time.sleep(0.1)
			self.logger.debug("Writing to memory now, waiting 10 seconds for this to complete...")
			self.pstdin.write("write mem\n")
			# @bee14 Must time this if using paramiko, "write mem" must fully execute.
			# I can't use pexpect (if using paramiko) to wait for prompt to come back, and the session closes if EOF is read early
			# Since I cannot test "write mem" after running all this privilege commands myself, 10 seems like a safe waiting time
			# I should honestly just use ssh subprocess and pexpect.  How does this look?
			time.sleep(10)  
		
		return "Commands have been run on the switch"
	
	def retrieveTerminalOutput(self):  
		self.logger.debug("Retrieving terminal input/output...")
		
		# With Paramiko, checking for specific output (ex: "END") is the only way to continue a session
		# while reading output.  There's no way to check for EOF, no pexpect functionality, and if you hit
		# EOF, of course the file closes.
		self.pstdin.write("echo END\n")	# how we're going to find the end of stdout before closing it by reading the EOF line

		exitLoop = False
		terminalOutput = "\nTerminal Input/Output: " 
		while (exitLoop == False):
			lineBytes = self.pstdout.readline()
			lineStr = lineBytes.decode(encoding='UTF-8'); 
			checkForEnd = lineStr[0:3]			
			if (checkForEnd == "END"):
				exitLoop = True
				terminalOutput = terminalOutput + lineStr
			elif (lineStr != ''):
				terminalOutput = terminalOutput + lineStr
				
		return terminalOutput
		

# MAIN Function - Handles 5 Tasks 
#  1 - Get username and password from input
#  2 - Retrieve permissions commands from a command file
#  3 - Query AssetDB for (almost) all Production 3750/3850 switches (Put into Hash Table)
#  4 - Spawn threads to SSH into each switch and run privilege commands
#  5 - Output logging results
def main():
	
##### (1) Get username and password 
	try:
		username = input("\nEnter AD username: ")
		password = getpass.getpass("Enter password: ")
	except KeyboardInterrupt:
		print("KeyboardInterrupt detected, terminating program")
		sys.exit(-1)
	
##### (2) Retrieve commands
	print ("\n\nRetrieving commands from configuration file...")
	logging.info("Retrieving commands from configuration file...")  #ccliff set streamhandler for INFO!
	
	commandTable = {}
	try:
		commandFile = open(RUN_COMMANDS_CONFIG_FILE, 'r')
		counter = 1
		for line in commandFile:
			if(line[0:1] == '#' or line == '\n' or line == ""):
				#Skip: lines starting with '#'
				#      blank lines '\n'
				#      also "", which may be the last blank line before EOF
				continue;
			else:
				commandTable[counter] = line.rstrip('\n')   # Get rid of "\n" if there is one
				counter += 1
	except IOError:
		print("File Open Error, terminating program") 
		sys.exit(-1)
	print("\t%d commands to be run" % len(commandTable));
	
	logging.info("Output Commands to Run...")   #ccliff - forget double comments, add StreamHandler to logger in future
	if (VERBOSE_MODE == True):
		print("\n\nOutput Commands to Run...")
	for cmd in commandTable.keys():
		logging.info("\t%s" % commandTable[cmd])
		if (VERBOSE_MODE == True):
			print("\t%s" % commandTable[cmd])
	
##### (3) Query AssetDB for all Prod 3750/3850 DNS names 
#####     Then, define a hash table, populate it with query result values
	print ("\nQuerying the AssetDB...")
	try:
		conn = pypyodbc.connect('Driver=FreeTDS;Server=136.142.3.12;port=1433;database=Asset;uid=reporterRO;pwd=N3tcool')
		result = conn.cursor().execute(ASSETDB_QUERY).fetchall()
	except Exception:
		print("\tError trying to connect to remote database, terminating program") 
		sys.exit(-1)
	# Add switches to a dictionary for easy reading.  This table will be passed by reference to each thread upon initialization,
	switchResultTable = {}
	for row in result:
		for field in row: 
			# Only the switch DNS names are being retrieved, will be the key for switchTable
			switchResultTable[field] = "NOT_RUN_YET"
	print("\t%d AssetDB entries retrieved\n" % len(switchResultTable));
	
	#testFail = "NotARealSwitch.cssd.pitt.edu"  # testcliff
	#switchResultTable[testFail] = "UNKOWN"
	
##### (4) For Loop to create thread for each switch
#####	  Each thread will SSH into each switch, run the commands, and write to memory
	print("Starting Threads for multiple SSH sessions...")
	threadList = []
	for switchName in switchResultTable.keys():
		threadName = "Thread_" + switchName;
		logFileName = "./switch output logs/" + switchName.rstrip(".net.pitt.edu") + "__Logging"
		
		# @tmp -> used for getting a sample time reading 
		start = time.time();
		
		t = threading.Thread(name=threadName, target=switchThread, args=(switchName, logFileName, username, password, commandTable, switchResultTable));
		#t = threading.Thread(name=threadName, target=switchThread, args=(switchName, logFileName, username, password, commandTable, 0)); 
		t.start();
		
		# @tmp -> 3 lines below just used for getting a sample time reading, t.join() line will be taken out of course
		t.join(); 
		end = time.time() - start;  
		print("\tTime taken: %d" % end);
		
		threadList.append(t)
		
		if(len(threadList) % 50 == 0):
			print("\tCreated %d threads so far, %d threads to go.." % (len(threadList), (len(switchResultTable) - len(threadList))));
		
		time.sleep(0.5); # There are 707 switches, and they are all taking a long time to run.  This should probably wait much longer
	
	#@bee14 this is how I was ensuring all threads have completed, but I don't believe I need this if I just wait a long time ha
	#print("\tAll threads have been started, waiting for threads to finish running...")
	#for t in threadList:
	#	t.join();
	#time.sleep(20)
	#print("\tAll threads have now completed\n")
	
##### (5) Output Results (put result status for each switch in a .csv file, user can check the logs for SSH sessions)
	try:
		# Open file, this will overwrite existing file if it exists (which we want)
		outputFile = open('results.csv', 'w')
		csvWriter = csv.writer(outputFile, quoting=csv.QUOTE_NONE)
	except IOError:
		print("File Open Error, terminating program") 
		sys.exit(-1)
	
	print ("Outputting results to \"results.csv\"......")
	failureCount = 0;
	resultSampleOutputCount = 3; 
	sampleOutputTable = "\t\tSwitch DNS Name,     \tProgram Result\n"
	csvWriter.writerow(["Switch DNS Name", "Program Result"])
	for switch in switchResultTable.keys():
		csvWriter.writerow([switch, switchResultTable[switch]])
		if(switchResultTable[switch] != "LOGIN_SUCCESS_COMMANDS_RUN"):
			failureCount+=1
		if(VERBOSE_MODE == True and resultSampleOutputCount != 0):
			sampleOutputTable += "\t" + switch + ",  " + switchResultTable[switch] + "\n"
			resultSampleOutputCount-=1
			
	if(VERBOSE_MODE == True):
		print(sampleOutputTable + "\t...see rest of results in the csv file...");
		
	if(failureCount > 0):
		print("\n\n\tIMPORTANT: %d SSH Login Failure(s) encountered." % failureCount)
		print("\tThese switches may not be important, but should be checked out in \n" \
		      "\t\"results.csv\", along with the each switch's individual log file.")
	else:
		print("\n\tNo SSH Login Failures were encountered.  See the individual switch logs if unsure \n" \
		      "\tthat all commands were run successfully on a particular switch.")
	
	print ("\n\nProgram is Complete.")
	print ("    Check \"ssh_session_results.csv\" to verify each switch was connected to, \n" \
		   "        and commands were at least run on the switch")
	print ("    For more in-depth information, check the log files in the \"switch output logs\".\n" \
		   "        For each switch, a thread logs the entire ssh session and command execution.  The \n" \
		   "        terminal output for each switch is also found here, so these log files will contain \n" \
		   "        any errors encountered while attempting to run the commands.")
	print ("    The main program logs are in \"main_program.log\". This file contains all logging, \n" \
		   "        from this program and every single switch thread.\n")

	# Close existing dynamic variables 
	conn.close()
	outputFile.close()
	return 0
	
# RUN
if __name__ == "__main__":
    main()





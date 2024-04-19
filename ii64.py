# Python 3

# this basically pretends to be the II/64 development cartridge
#
# current directory needs to contain (links to):
#	mame executable				"./mame"
#	mame ROM directory for Apple IIe	"./roms"
#	mame audio sample directory		"./samples"
#	lua script for mame			"./ii64.lua"
#	Apple IIe disk images from PAN		"./Disk23.dsk"
#	Apple IIe demo disk image from PAN	"./demodisk.dsk"
#	vice x64 executable			"./x64"
#	vice data (ROM) directory		"./x64data"

import os
import sys
import time
import socket
import struct
import subprocess

SUPPRESSRESET = 1.0	# how many secs before reset can be triggered again
STEP = True		# interpret "T" command as sTep, not just sTatus
KEEPRUNNING = True	# II/64 should keep executing after each command?

dbgprint = print

def mkfifo(fifoname):
	try:
		os.mkfifo(fifoname)
	except FileExistsError:
		pass
	except IOError as e:
		print(f'mkfifo {fifoname}: {e.strerror}', file=sys.stderr)
		sys.exit()

def run(L):
	try:
		p = subprocess.Popen(L)
	except IOError as e:
		print(f'{L[0]}: {e.strerror}', file=sys.stderr)
		sys.exit()
	return p

def setup():
	global pipein, pipeout, c64emu

	# listens at TCP/6502 by default
	run(['./x64', '-directory', 'x64data', '-binarymonitor'])	

	mkfifo('mamein')
	mkfifo('mameout')

	run(['./mame',
		'-resolution', '1120x768',
		'-autoboot_script', 'ii64.lua',
		'-window', '-skip_gameinfo', '-rompath', 'roms',
		'-samplepath', 'samples',
		'apple2e', '-flop1', 'Disk23.dsk',
		'-flop2', 'demodisk.dsk'
	])

	# has to be after run() because they block on open, and opened in
	# this order to avoid deadlocking due to lua script opening order
	pipeout = open('mamein', 'wb', buffering=0)
	pipein = open('mameout', 'rb')

	c64emu = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	c64emu.connect( ('localhost', 6502) )
	c64_getregs()
	c64_getbank()
	c64_run()

c64_regs = {}
c64_bankid = None
c64_reqid = 0xdeadbeef

def c64_cmd(cmd, cmdbody):
	global c64_reqid

	c64emu.send(struct.pack('<BB L L B',
		0x02,		# STX
		0x02,		# API version
		len(cmdbody),	# length
		c64_reqid,	# cmd id
		cmd		# cmd
	) + cmdbody)

	c64_reqid += 1

	while True:
		# XXX relies on "nice" socket behavior on localhost
		data = c64emu.recv(12)
		assert len(data) == 12
		(stx, api, bodylen, type, error, reqid) = struct.unpack('<BB L BB L', data)
		assert stx == 0x02
		assert api == 0x02
		if reqid == 0xffffffff:
			# not the response to the command sent - toss body data
			c64emu.recv(bodylen)
			continue
		assert c64_reqid-1 == reqid
		assert error == 0x00, f'error {error:02x}'
		data = c64emu.recv(bodylen)
		return type, data

def c64_getregs():
	# map from Vice's names to internal ones
	VICEREGS = {
		'PC':	'PC',
		'A':	'A',
		'X':	'X',
		'Y':	'Y',
		'SP':	'SP',
		'FL':	'P',
	}

	# query available regs for main memory space
	type, data = c64_cmd(0x83, b'\x00')
	assert type == 0x83

	i = 2
	nregs = struct.unpack_from('<H', data)[0]
	for regs in range(nregs):
		n = data[i]
		id = data[i+1]
		bits = data[i+2]
		namelen = data[i+3]
		name = str(data[i+4:i+4+namelen], encoding='ascii')
		i += namelen + 4
		#print(f'{n} {id} {bits} {name}')

		# save their id numbers for later
		if name in VICEREGS:
			c64_regs[VICEREGS[name]] = id

	assert len(c64_regs) == 6

def c64_getbank():
	global c64_bankid
	VICENAME = 'default'

	# query available banks
	type, data = c64_cmd(0x82, b'')
	assert type == 0x82

	i = 2
	nbanks = struct.unpack_from('<H', data)[0]
	for bank in range(nbanks):
		n = data[i]
		id = struct.unpack_from('<H', data[i+1:])[0]
		namelen = data[i+3]
		name = str(data[i+4:i+4+namelen], encoding='ascii')
		i += n + 1
		#print(f'{n} {id} {name}')

		if name == VICENAME:
			c64_bankid = id
			break

def c64_run():
	# monitor exit
	type, data = c64_cmd(0xaa, b'')
	assert type == 0xaa

class C64Exception(BaseException):	pass
class C64NMI(C64Exception):		pass
class C64Reset(C64Exception):		pass

RISING = 1
FALLING = 0

class State:
	def __init__(self):
		self.nmi = 0
		self.resettime = 0.0
		self.shiftreg = self.inbits = self.outbits = 0
state = State()

def dowire(mask=0, edge=None):
	while True:
		s = pipein.read(1)
		if len(s) == 0:
			# exit nicely if mame's finished
			sys.exit()

		ch = s[0]
		# XXX should switch to match when more recent Python available
		if ch == 0x40:
			now = time.time()
			if now > state.resettime + SUPPRESSRESET:
				state.resettime = now
				raise C64Reset()

		elif ch in (0x61, 0x62, 0x63):
			pipeout.write(bytes([state.outbits + ord('0')]))

		elif ch in (0x58, 0x5a, 0x5c):
			shift = 2 - ((ch >> 1) & 0b11)
			oldinbits = state.inbits
			state.inbits &= ~(1 << shift)
			if edge == FALLING and \
			   (oldinbits & mask) and \
			   not (state.inbits & mask):
				return
		elif ch in (0x59, 0x5b, 0x5d):
			shift = 2 - ((ch >> 1) & 0b11)
			oldinbits = state.inbits
			state.inbits |= 1 << shift
			if edge == RISING and \
			   not (oldinbits & mask) and \
			   (state.inbits & mask):
				return

		elif ch == 0x5f:
			state.nmi = 1
		elif ch == 0x5e:
			if state.nmi == 1:
				state.nmi = 0
				raise C64NMI()

		else:
			print(f'unhandled read: 0x{ch:02x}')
			assert False

def handshake():
	try:
		dowire()
	except C64NMI:
		if state.inbits != 0b010:
			# unexpected value, re-raise the exception
			raise
		state.outbits = state.inbits

		dowire(1, RISING)
		if state.inbits != 0b011:
			# unexpected value, re-raise the exception
			raise
		state.outbits = state.inbits

def shift2():
	dowire(0b100, RISING)
	state.outbits |= 0b100

	twoin = state.inbits & 0b11
	twoout = (state.shiftreg >> 6) & 0b11
	state.outbits = (state.outbits & ~0b11) | twoout
	state.shiftreg = ((state.shiftreg << 2) | twoin) & 0xff

	dowire(0b100, FALLING)
	state.outbits &= ~0b100

def getbyte():
	shift2()
	shift2()
	shift2()
	shift2()
	return state.shiftreg

def putbyte(b):
	state.shiftreg = b
	shift2()
	shift2()
	shift2()
	shift2()

def docart():
	while True:
		try:
			handshake()
			cmd = getbyte()

			name = f'command_{cmd}'
			if name not in globals():
				print(f'unknown command 0x{cmd:02x}')
				continue
			dbgprint(name)
			globals()[name]()

		except C64Reset:
			print('reset detected')
			# hard reset
			type, data = c64_cmd(0xcc, b'\x01')
			assert type == 0xcc
		except C64NMI:
			# NMI at unexpected time - try restarting loop
			pass

def command_0():
	# read start/end c64 addresses
	start = getbyte()
	start |= getbyte() << 8
	end = getbyte()
	end |= getbyte() << 8
	dbgprint(f'start {start:04x}, end {end:04x}')

	type, data = c64_cmd(0x01, struct.pack('<B HH B H',
		1,		# side effects ok - II/64 couldn't prevent them
		start,		# start and end addrs
		end,
		0x00,		# main memory
		c64_bankid	# default bank
	))
	assert type == 0x01

	for b in data[2:]:
		putbyte(b)
		dbgprint(f'{b:02x} ', end='')
	dbgprint()

	if KEEPRUNNING:
		c64_run()

def command_1():
	# write start/end c64 addresses
	start = getbyte()
	start |= getbyte() << 8
	end = getbyte()
	end |= getbyte() << 8
	dbgprint(f'start {start:04x}, end {end:04x}')

	wdata = bytearray()
	for i in range(start, end+1):
		wdata.append(getbyte())
		dbgprint(f'{wdata[-1]:02x} ', end='')
	dbgprint()

	type, data = c64_cmd(0x02, struct.pack('<B HH B H',
		1,		# side effects ok - II/64 couldn't prevent them
		start,		# start and end addrs
		end,
		0x00,		# main memory
		c64_bankid	# default bank
	) + wdata)
	assert type == 0x02

	if KEEPRUNNING:
		c64_run()

def command_2():
	# exec start c64 address
	start = getbyte()
	start |= getbyte() << 8
	dbgprint(f'start {start:04x}')

	# set PC to start address
	type, data = c64_cmd(0x32, struct.pack('<BH BBH',
		0x00,		# main memory space
		1,		# number of regs to set
		3,		# size of reg value + reg id
		c64_regs['PC'],	# PC's id
		start		# new PC value
	))
	assert type == 0x31

	# and go!
	c64_run()

def command_3():
	if STEP:
		# step one instr before returning reg info
		type, data = c64_cmd(0x71, b'\x00\x01\x00')
		assert type == 0x71

	# get registers for main memory
	type, data = c64_cmd(0x31, b'\x00')
	assert type == 0x31

	i = 2
	values = {}
	nregs = struct.unpack_from('<H', data)[0]
	for regs in range(nregs):
		n = data[i]
		id = data[i+1]
		if n == 1+1:
			value = data[i+2]
		elif n == 1+2:
			value = struct.unpack_from('<H', data[i+2:])[0]
		else:
			assert False
		i += n + 1

		values[id] = value

	putbyte(values[c64_regs['SP']])
	putbyte(values[c64_regs['P']])
	putbyte(values[c64_regs['PC']] & 0xff)
	putbyte(values[c64_regs['PC']] >> 8)
	putbyte(values[c64_regs['A']])
	putbyte(values[c64_regs['X']])
	putbyte(values[c64_regs['Y']])

	# doesn't make sense to step but then start running right away
	if KEEPRUNNING and not STEP:
		c64_run()

def main():
	setup()
	docart()

if __name__ == '__main__':
	main()

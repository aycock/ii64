-- this Lua script is run by the Python handler

-- these should be named pipes to communicate with Python handler
INFILE = 'mamein'
OUTFILE = 'mameout'

io = require('io')
string = require('string')

function doout(offset, data, mask)
	-- output lsb of address as an ASCII char
	ofile:write(string.char(offset & 0xff))
end

function dooutin(offset, data, mask)
	-- output lsb of address as an ASCII char
	ofile:write(string.char(offset & 0xff))

	-- '0'...'7' ASCII chars indicate 3x bit settings for c06[123]
	s = ifile:read(1)
	mask = 1 << (2 - ((offset & 3) - 1))
	if (string.byte(s) & mask) == 0 then
		return 0
	else
		return 0x80
	end
end

print('Starting II/64 monitoring in MAME...')

ifile = io.open(INFILE, 'r')
-- ifile:setvbuf('no')

ofile = io.open(OUTFILE, 'w')
ofile:setvbuf('no')

space = manager.machine.devices[':maincpu'].spaces['program']

space:install_read_tap(0xc040, 0xc040, 'c040', doout)
space:install_read_tap(0xc058, 0xc05f, 'c05x', doout)
space:install_read_tap(0xc061, 0xc063, 'c06x', dooutin)

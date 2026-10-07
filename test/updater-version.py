#!/usr/bin/env python3
"""Execute the stock updater's MIPS version-format gate, with strchr mocked.
Pass the extracted audited stock demo. No device or emulator library is required.
"""
import pathlib, struct, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'tools'))
from build import DEMO_SHA, VERSION, fileoff, sha, version_tag

raw = pathlib.Path(sys.argv[1]).read_bytes()
assert sha(raw) == DEMO_SHA, 'Expected the audited stock executable'

def word(address): return struct.unpack_from('<I', raw, fileoff(raw, address))[0]

def validate(tag):
    # Execute only the stock strchr(version, 'V') gate. Its failure branch joins the
    # same cleanup that returns -13 (check error); success continues to version equality.
    registers = [0] * 32
    registers[28], registers[20], registers[29] = 0xa26cc0, 0x1001000, 0x7000f000
    strchr = word(registers[28] - 0x864)
    pc, pending = 0x4f8418, None
    for _ in range(16):
        if pc == 0x4f829c:
            assert word(0x4f82ac) == 0x2403fff3  # addiu v1, zero, -13
            return -13
        if pc == 0x4f8430: return 1
        instruction = word(pc)
        opcode = instruction >> 26
        rs, rt, rd = (instruction >> 21) & 31, (instruction >> 16) & 31, (instruction >> 11) & 31
        immediate = instruction & 65535
        signed = immediate if immediate < 32768 else immediate - 65536
        previous, pending = pending, None
        if opcode == 0x23:  # lw
            address = registers[rs] + signed
            registers[rt] = 0xa26cc0 if address == 0x7000f018 else word(address)
        elif opcode == 9:  # addiu
            registers[rt] = (registers[rs] + signed) & 0xffffffff
        elif opcode == 0 and instruction & 63 == 0x25:  # or / move
            registers[rd] = registers[rs] | registers[rt]
        elif opcode == 0 and instruction & 63 == 9:  # jalr, followed by its delay slot
            assert registers[rs] == strchr, 'Unexpected libc call in version gate'
            registers[rd] = pc + 8
            pending = 'strchr'
        elif opcode == 4:  # beq, followed by its delay slot
            pending = pc + 4 + signed * 4 if registers[rs] == registers[rt] else pc + 8
        else:
            raise AssertionError(f'Unsupported instruction {instruction:08x} at {pc:x}')
        pc += 4
        if previous == 'strchr':
            assert registers[4] == 0x1001000
            index = tag.find(chr(registers[5]))
            registers[2] = 0x1001000 + index if index >= 0 else 0
        elif previous is not None:
            pc = previous
    raise AssertionError('Instruction budget exhausted')

assert validate('D002i') == -13
assert validate('V8.7i') == 1
assert validate('V002i') == 1
for tag in (version_tag(VERSION, 'ipod', 2), version_tag(VERSION, 'stock', 46655),
            version_tag('8.10', 'ipod')):
    verdict = validate(tag)
    print(f'{tag}: {verdict}', flush=True)
    assert verdict == 1, f'Stock updater rejected {tag} with check error {verdict}'
print('Stock updater format gate accepts generated development and production tags.')

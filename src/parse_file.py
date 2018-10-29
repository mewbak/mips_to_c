import attr
import re

import typing
from typing import List, Union, Iterator, Optional, Dict, Callable, Any

from options import Options
from parse_instruction import Instruction, parse_instruction

@attr.s(frozen=True)
class Label:
    name: str = attr.ib()

    def __str__(self) -> str:
        return f'  .{self.name}:'

@attr.s
class Function:
    name: str = attr.ib()
    body: List[Union[Instruction, Label]] = attr.ib(factory=list)
    jumptable_labels: List[Label] = attr.ib(factory=list)

    def new_label(self, name: str) -> None:
        self.body.append(Label(name))

    def new_jumptable_label(self, name: str) -> None:
        self.body.append(Label(name))
        self.jumptable_labels.append(Label(name))

    def new_instruction(self, instruction: Instruction) -> None:
        self.body.append(instruction)

    def __str__(self) -> str:
        body = "\n".join(str(item) for item in self.body)
        return f'glabel {self.name}\n{body}'

@attr.s
class MIPSFile:
    filename: str = attr.ib()
    functions: List[Function] = attr.ib(factory=list)
    current_function: Optional[Function] = attr.ib(default=None, repr=False)

    def new_function(self, name: str) -> None:
        self.current_function = Function(name=name)
        self.functions.append(self.current_function)

    def new_instruction(self, instruction: Instruction) -> None:
        assert self.current_function is not None
        self.current_function.new_instruction(instruction)

    def new_label(self, label_name: str) -> None:
        assert self.current_function is not None
        self.current_function.new_label(label_name)

    def new_jumptable_label(self, label_name: str) -> None:
        assert self.current_function is not None
        self.current_function.new_jumptable_label(label_name)

    def __str__(self) -> str:
        functions_str = '\n\n'.join(str(function) for function in self.functions)
        return f'# {self.filename}\n{functions_str}'


def parse_file(f: typing.TextIO, options: Options) -> MIPSFile:
    mips_file: MIPSFile = MIPSFile(options.filename)

    for line in f:
        # Strip comments and whitespace
        line = re.sub(r'/\*.*\*/', '', line)
        line = re.sub(r'#.*$', '', line)
        line = line.strip()

        if line == '':
            continue
        elif line.startswith('.') and line.endswith(':'):
            # Label.
            label_name: str = line.strip('.:')
            mips_file.new_label(label_name)
        elif line.startswith('.'):
            # Assembler directive.
            pass
        elif line.startswith('glabel'):
            # Function label.
            function_name: str = line.split(' ')[1]
            if re.match('L[0-9A-F]{8}', function_name):
                mips_file.new_jumptable_label(function_name)
            else:
                mips_file.new_function(function_name)
        else:
            # Instruction.
            instruction: Instruction = parse_instruction(line)
            mips_file.new_instruction(instruction)

    return mips_file

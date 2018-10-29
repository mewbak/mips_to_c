import attr
import sys
import traceback

import typing
from typing import List, Union, Iterator, Optional, Dict, Callable, Tuple, Any, Set

from options import Options
from parse_instruction import (
    AsmGlobalSymbol, AsmAddressMode, AsmLiteral,
    Instruction, Register,
    Argument, BinOp, Macro
)
from flow_graph import (
    FlowGraph,
    Function,
    Node, ReturnNode,
    build_callgraph
)

ARGUMENT_REGS = list(map(Register, [
    'a0', 'a1', 'a2', 'a3',
    'f12', 'f14'
]))

# TODO: include temporary floating-point registers
CALLER_SAVE_REGS = ARGUMENT_REGS + list(map(Register, [
    'at',
    't0', 't1', 't2', 't3', 't4', 't5', 't6', 't7', 't8', 't9',
    'hi', 'lo', 'condition_bit', 'return_reg'
]))

CALLEE_SAVE_REGS = list(map(Register, [
    's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7',
]))

SPECIAL_REGS = list(map(Register, [
    'ra', '31', 'fp'
]))


@attr.s(cmp=False, repr=False)
class Type:
    """
    Type information for an expression, which may improve over time. The least
    specific type is any (initially the case for e.g. arguments); this might
    get refined into intish if the value gets used for e.g. an integer add
    operation, or into u32 if it participates in a logical right shift.
    Types cannot change except for improvements of this kind -- thus concrete
    types like u32 can never change into anything else, and e.g. ints can't
    become floats.
    """
    K_INT = 1
    K_PTR = 2
    K_FLOAT = 4
    K_INTPTR = 3
    K_ANY = 7
    SIGNED = 1
    UNSIGNED = 2
    ANY_SIGN = 3

    kind: int = attr.ib()
    size: Optional[int] = attr.ib()
    sign: int = attr.ib()
    uf_parent: Optional['Type'] = attr.ib(default=None)

    def unify(self, other: 'Type') -> bool:
        """
        Try to set this type equal to another. Returns true on success.
        Once set equal, the types will always be equal (we use a union-find
        structure to ensure this).
        """
        x = self.get_representative()
        y = other.get_representative()
        if x is y:
            return True
        if x.size is not None and y.size is not None and x.size != y.size:
            return False
        size = x.size if x.size is not None else y.size
        kind = x.kind & y.kind
        sign = x.sign & y.sign
        if size in [8, 16]:
            kind &= ~Type.K_FLOAT
        if size in [8, 16, 64]:
            kind &= ~Type.K_PTR
        if kind == 0 or sign == 0:
            return False
        if kind == Type.K_PTR:
            size = 32
        if sign != Type.ANY_SIGN:
            assert kind == Type.K_INT
        x.kind = kind
        x.size = size
        x.sign = sign
        y.uf_parent = x
        return True

    def get_representative(self) -> 'Type':
        if self.uf_parent is None:
            return self
        self.uf_parent = self.uf_parent.get_representative()
        return self.uf_parent

    def is_float(self) -> bool:
        return self.get_representative().kind == Type.K_FLOAT

    def is_pointer(self) -> bool:
        return self.get_representative().kind == Type.K_PTR

    def is_unsigned(self) -> bool:
        return self.get_representative().sign == Type.UNSIGNED

    def is_any(self) -> bool:
        return str(self) == '?'

    def get_size(self) -> int:
        return self.get_representative().size or 32

    def to_decl(self) -> str:
        ret = str(self)
        return ret if ret.endswith('*') else ret + ' '

    def __str__(self) -> str:
        type = self.get_representative()
        size = type.size or 32
        sign = 's' if type.sign & Type.SIGNED else 'u'
        if type.kind == Type.K_ANY:
            if type.size is not None:
                return f'?{size}'
            return '?'
        if type.kind == Type.K_PTR:
            return 'void *'
        if type.kind == Type.K_FLOAT:
            return f'f{size}'
        return f'{sign}{size}'

    def __repr__(self) -> str:
        type = self.get_representative()
        signstr = (('+' if type.sign & Type.SIGNED else '') +
                   ('-' if type.sign & Type.UNSIGNED else ''))
        kindstr = (('I' if type.kind & Type.K_INT else '') +
                   ('P' if type.kind & Type.K_PTR else '') +
                   ('F' if type.kind & Type.K_FLOAT else ''))
        sizestr = str(type.size) if type.size is not None else '?'
        return f'Type({signstr + kindstr + sizestr})'

    @staticmethod
    def any() -> 'Type':
        return Type(kind=Type.K_ANY, size=None, sign=Type.ANY_SIGN)

    @staticmethod
    def intish() -> 'Type':
        return Type(kind=Type.K_INT, size=None, sign=Type.ANY_SIGN)

    @staticmethod
    def intptr() -> 'Type':
        return Type(kind=Type.K_INTPTR, size=None, sign=Type.ANY_SIGN)

    @staticmethod
    def ptr() -> 'Type':
        return Type(kind=Type.K_PTR, size=32, sign=Type.ANY_SIGN)

    @staticmethod
    def f32() -> 'Type':
        return Type(kind=Type.K_FLOAT, size=32, sign=Type.ANY_SIGN)

    @staticmethod
    def f64() -> 'Type':
        return Type(kind=Type.K_FLOAT, size=64, sign=Type.ANY_SIGN)

    @staticmethod
    def s32() -> 'Type':
        return Type(kind=Type.K_INT, size=32, sign=Type.SIGNED)

    @staticmethod
    def u32() -> 'Type':
        return Type(kind=Type.K_INT, size=32, sign=Type.UNSIGNED)

    @staticmethod
    def u64() -> 'Type':
        return Type(kind=Type.K_INT, size=64, sign=Type.UNSIGNED)

    @staticmethod
    def of_size(size: int) -> 'Type':
        return Type(kind=Type.K_ANY, size=size, sign=Type.ANY_SIGN)

    @staticmethod
    def bool() -> 'Type':
        return Type.intish()

def as_type(expr: 'Expression', type: Type, silent: bool) -> 'Expression':
    if expr.type.unify(type):
        if not silent:
            return Cast(expr=expr, reinterpret=True, silent=False, type=type)
        return expr
    return Cast(expr=expr, reinterpret=True, silent=False, type=type)

def as_f32(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.f32(), True)

def as_f64(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.f64(), True)

def as_s32(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.s32(), False)

def as_u32(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.u32(), False)

def as_intish(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.intish(), True)

def as_intptr(expr: 'Expression') -> 'Expression':
    return as_type(expr, Type.intptr(), True)


@attr.s
class StackInfo:
    function: Function = attr.ib()
    allocated_stack_size: int = attr.ib(default=0)
    is_leaf: bool = attr.ib(default=True)
    local_vars_region_bottom: int = attr.ib(default=0)
    return_addr_location: int = attr.ib(default=0)
    callee_save_reg_locations: Dict[Register, int] = attr.ib(factory=dict)
    unique_type_map: Dict[Any, 'Type'] = attr.ib(factory=dict)
    local_vars: List['LocalVar'] = attr.ib(factory=list)
    temp_vars: List['EvalOnceStmt'] = attr.ib(factory=list)
    phi_vars: List['PhiExpr'] = attr.ib(factory=list)
    arguments: List['PassedInArg'] = attr.ib(factory=list)
    temp_name_counter: Dict[str, int] = attr.ib(factory=dict)

    def temp_var_generator(self, prefix: str) -> Callable[[], str]:
        def gen() -> str:
            counter = self.temp_name_counter.get(prefix, 0) + 1
            self.temp_name_counter[prefix] = counter
            return f'temp_{prefix}' + (f'_{counter}' if counter > 1 else '')
        return gen

    def in_subroutine_arg_region(self, location: int) -> bool:
        assert not self.is_leaf
        if self.callee_save_reg_locations:
            subroutine_arg_top = min(self.callee_save_reg_locations.values())
            assert self.return_addr_location > subroutine_arg_top
        else:
            subroutine_arg_top = self.return_addr_location

        return location < subroutine_arg_top

    def in_local_var_region(self, location: int) -> bool:
        return self.local_vars_region_bottom <= location < self.allocated_stack_size

    def location_above_stack(self, location: int) -> bool:
        return location >= self.allocated_stack_size

    def add_local_var(self, var: 'LocalVar') -> None:
        if any(v.value == var.value for v in self.local_vars):
            return
        self.local_vars.append(var)
        # Make sure the local vars stay sorted in order on the stack.
        self.local_vars.sort(key=lambda v: v.value)

    def add_argument(self, arg: 'PassedInArg') -> None:
        if any(a.value == arg.value for a in self.arguments):
            return
        self.arguments.append(arg)
        self.arguments.sort(key=lambda a: a.value)

    def get_argument(self, location: int) -> 'PassedInArg':
        return PassedInArg(location, copied=True,
                type=self.unique_type_for('arg', location))

    def unique_type_for(self, category: str, key: Any) -> 'Type':
        key = (category, key)
        if key not in self.unique_type_map:
            self.unique_type_map[key] = Type.any()
        return self.unique_type_map[key]

    def global_symbol(self, sym: AsmGlobalSymbol) -> 'GlobalSymbol':
        return GlobalSymbol(symbol_name=sym.symbol_name,
                type=self.unique_type_for('symbol', sym.symbol_name))

    def get_stack_var(self, location: int, store: bool) -> 'Expression':
        if self.in_local_var_region(location):
            return LocalVar(location,
                    type=self.unique_type_for('stack', location))
        elif self.location_above_stack(location):
            ret = self.get_argument(location - self.allocated_stack_size)
            if not store:
                self.add_argument(ret)
            return ret
        elif self.in_subroutine_arg_region(location):
            return SubroutineArg(location, type=Type.any())
        else:
            # Some annoying bookkeeping instruction. To avoid
            # further special-casing, just return whatever - it won't matter.
            return LocalVar(location,
                    type=self.unique_type_for('stack', location))

    def __str__(self) -> str:
        return '\n'.join([
            f'Stack info for function {self.function.name}:',
            f'Allocated stack size: {self.allocated_stack_size}',
            f'Leaf? {self.is_leaf}',
            f'Bottom of local vars region: {self.local_vars_region_bottom}',
            f'Location of return addr: {self.return_addr_location}',
            f'Locations of callee save registers: {self.callee_save_reg_locations}'
        ])

def get_stack_info(function: Function, start_node: Node) -> StackInfo:
    info = StackInfo(function)

    # The goal here is to pick out special instructions that provide information
    # about this function's stack setup.
    for inst in start_node.block.instructions:
        if not inst.args:
            continue

        destination = typing.cast(Register, inst.args[0])

        if inst.mnemonic == 'addiu' and destination.register_name == 'sp':
            # Moving the stack pointer.
            assert isinstance(inst.args[2], AsmLiteral)
            info.allocated_stack_size = abs(inst.args[2].value)
        elif inst.mnemonic == 'sw' and destination.register_name == 'ra':
            # Saving the return address on the stack.
            assert isinstance(inst.args[1], AsmAddressMode)
            assert inst.args[1].rhs.register_name == 'sp'
            info.is_leaf = False
            if inst.args[1].lhs:
                assert isinstance(inst.args[1].lhs, AsmLiteral)
                info.return_addr_location = inst.args[1].lhs.value
            else:
                # Note that this should only happen in the rare case that
                # this function only calls subroutines with no arguments.
                info.return_addr_location = 0
        elif (inst.mnemonic == 'sw' and
              destination.is_callee_save() and
              isinstance(inst.args[1], AsmAddressMode) and
              inst.args[1].rhs.register_name == 'sp'):
            # Initial saving of callee-save register onto the stack.
            assert isinstance(inst.args[1].rhs, Register)
            if inst.args[1].lhs:
                assert isinstance(inst.args[1].lhs, AsmLiteral)
                info.callee_save_reg_locations[destination] = inst.args[1].lhs.value
            else:
                info.callee_save_reg_locations[destination] = 0

    # Find the region that contains local variables.
    if info.is_leaf and info.callee_save_reg_locations:
        # In a leaf with callee-save registers, the local variables
        # lie directly above those registers.
        info.local_vars_region_bottom = max(info.callee_save_reg_locations.values()) + 4
    elif info.is_leaf:
        # In a leaf without callee-save registers, the local variables
        # lie directly at the bottom of the stack.
        info.local_vars_region_bottom = 0
    else:
        # In a non-leaf, the local variables lie above the location of the
        # return address.
        info.local_vars_region_bottom = info.return_addr_location + 4

    # Done.
    return info

def format_hex(val: int) -> str:
    return format(val, 'x').upper()


@attr.s(frozen=True, cmp=False)
class BinaryOp:
    left: 'Expression' = attr.ib()
    op: str = attr.ib()
    right: 'Expression' = attr.ib()
    type: Type = attr.ib()

    @staticmethod
    def int(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_intish(left), op=op, right=as_intish(right),
                type=Type.intish())

    @staticmethod
    def intptr(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_intptr(left), op=op, right=as_intptr(right),
                type=Type.intptr())

    @staticmethod
    def icmp(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_intptr(left), op=op, right=as_intptr(right),
                type=Type.bool())

    @staticmethod
    def ucmp(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_u32(left), op=op, right=as_u32(right),
                type=Type.bool())

    @staticmethod
    def fcmp(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_f32(left), op=op, right=as_f32(right),
                type=Type.bool())

    @staticmethod
    def dcmp(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_f64(left), op=op, right=as_f64(right),
                type=Type.bool())

    @staticmethod
    def s32(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_s32(left), op=op, right=as_s32(right),
                type=Type.s32())

    @staticmethod
    def u32(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_u32(left), op=op, right=as_u32(right),
                type=Type.u32())

    @staticmethod
    def f32(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_f32(left), op=op, right=as_f32(right),
                type=Type.f32())

    @staticmethod
    def f64(left: 'Expression', op: str, right: 'Expression') -> 'BinaryOp':
        return BinaryOp(left=as_f64(left), op=op, right=as_f64(right),
                type=Type.f64())

    def is_boolean(self) -> bool:
        return self.op in ['==', '!=', '>', '<', '>=', '<=']

    def negated(self) -> 'BinaryOp':
        assert self.is_boolean()
        return BinaryOp(
            left=self.left,
            op={
                '==': '!=',
                '!=': '==',
                '>' : '<=',
                '<' : '>=',
                '>=':  '<',
                '<=':  '>',
            }[self.op],
            right=self.right,
            type=Type.bool()
        )

    def dependencies(self) -> List['Expression']:
        return [self.left, self.right]

    def __str__(self) -> str:
        return f'({self.left} {self.op} {self.right})'

@attr.s(frozen=True, cmp=False)
class UnaryOp:
    op: str = attr.ib()
    expr: 'Expression' = attr.ib()
    type: Type = attr.ib()

    def dependencies(self) -> List['Expression']:
        return [self.expr]

    def __str__(self) -> str:
        return f'{self.op}{self.expr}'

@attr.s(frozen=True, cmp=False)
class Cast:
    expr: 'Expression' = attr.ib()
    type: Type = attr.ib()
    reinterpret: bool = attr.ib(default=False)
    silent: bool = attr.ib(default=True)

    def dependencies(self) -> List['Expression']:
        return [self.expr]

    def __str__(self) -> str:
        if self.reinterpret and (self.silent or is_type_obvious(self.expr)):
            return str(self.expr)
        if (self.reinterpret and
                self.expr.type.is_float() != self.type.is_float()):
            # This shouldn't happen, but mark it in the output if it does.
            return f'(bitwise {self.type}) {self.expr}'
        return f'({self.type}) {self.expr}'

@attr.s(frozen=True, cmp=False)
class FuncCall:
    func_name: str = attr.ib()
    args: List['Expression'] = attr.ib()
    type: Type = attr.ib()

    def dependencies(self) -> List['Expression']:
        return self.args

    def __str__(self) -> str:
        return f'{self.func_name}({", ".join(str(arg) for arg in self.args)})'

@attr.s(frozen=True, cmp=True)
class LocalVar:
    value: int = attr.ib()
    type: Type = attr.ib(cmp=False)

    def dependencies(self) -> List['Expression']:
        return []

    def __str__(self) -> str:
        return f'sp{format_hex(self.value)}'

@attr.s(frozen=True, cmp=True)
class PassedInArg:
    value: int = attr.ib()
    copied: bool = attr.ib(cmp=False)
    type: Type = attr.ib(cmp=False)

    def dependencies(self) -> List['Expression']:
        return []

    def __str__(self) -> str:
        if self.value % 4 == 0:
            return f'arg{format_hex(self.value // 4)}'
        else:
            return f'arg_unaligned{format_hex(self.value)}'

@attr.s(frozen=True, cmp=True)
class SubroutineArg:
    value: int = attr.ib()
    type: Type = attr.ib(cmp=False)

    def dependencies(self) -> List['Expression']:
        return []

    def __str__(self) -> str:
        return f'subroutine_arg{format_hex(self.value // 4)}'

@attr.s(frozen=True, cmp=True)
class StructAccess:
    # This has cmp=True since it represents a live expression and not an access
    # at a certain point in time -- this sometimes helps get rid of phi nodes.
    # Really it should represent the latter, but making that so is hard.
    struct_var: 'Expression' = attr.ib()
    offset: int = attr.ib()
    type: Type = attr.ib(cmp=False)

    def dependencies(self) -> List['Expression']:
        return [self.struct_var]

    def __str__(self) -> str:
        # TODO: don't treat offset == 0 specially if there have been other
        # non-zero-offset accesses for the same struct_var
        if isinstance(self.struct_var, AddressOf):
            if self.offset == 0:
                return f'{self.struct_var.expr}'
            else:
                return f'{self.struct_var.expr}.unk{format_hex(self.offset)}'
        else:
            if self.offset == 0:
                return f'*{self.struct_var}'
            else:
                return f'{self.struct_var}->unk{format_hex(self.offset)}'

@attr.s(frozen=True, cmp=True)
class GlobalSymbol:
    symbol_name: str = attr.ib()
    type: Type = attr.ib(cmp=False)

    def dependencies(self) -> List['Expression']:
        return []

    def __str__(self) -> str:
        return self.symbol_name

@attr.s(frozen=True, cmp=True)
class Literal:
    value: int = attr.ib()
    type: Type = attr.ib(cmp=False, factory=Type.any)

    def dependencies(self) -> List['Expression']:
        return []

    def __str__(self) -> str:
        if self.type.is_float():
            if self.type.get_size() == 32:
                return f'{parse_f32_imm(self.value)}f'
            else:
                return f'{parse_f64_imm(self.value)}'
        prefix = ''
        if self.type.is_pointer():
            if self.value == 0:
                return 'NULL'
            else:
                prefix = '(void *)'
        elif self.type.get_size() == 8:
            prefix = '(u8)'
        elif self.type.get_size() == 16:
            prefix = '(u16)'
        suffix = 'U' if self.type.is_unsigned() else ''
        mid = str(self.value) if abs(self.value) < 10 else hex(self.value)
        return prefix + mid + suffix

@attr.s(frozen=True, cmp=True)
class AddressOf:
    expr: 'Expression' = attr.ib()
    type: Type = attr.ib(cmp=False, factory=Type.ptr)

    def dependencies(self) -> List['Expression']:
        return [self.expr]

    def __str__(self) -> str:
        return f'&{self.expr}'

@attr.s(frozen=True)
class AddressMode:
    offset: int = attr.ib()
    rhs: Register = attr.ib()

    def __str__(self) -> str:
        if self.offset:
            return f'{self.offset}({self.rhs})'
        else:
            return f'({self.rhs})'

@attr.s(frozen=False, cmp=False)
class EvalOnceExpr:
    wrapped_expr: 'Expression' = attr.ib()
    var: Union[str, Callable[[], str]] = attr.ib(repr=False)
    always_emit: bool = attr.ib()
    type: Type = attr.ib()
    num_usages: int = attr.ib(default=0)

    def dependencies(self) -> List['Expression']:
        return [self.wrapped_expr]

    def get_var_name(self) -> str:
        if not isinstance(self.var, str):
            self.var = self.var()
        return self.var

    def use(self) -> None:
        self.num_usages += 1
        if self.num_usages == 1 and not self.always_emit:
            mark_used(self.wrapped_expr)

    def __str__(self) -> str:
        if self.num_usages <= 1:
            return str(self.wrapped_expr)
        else:
            return self.get_var_name()

@attr.s(frozen=False, cmp=False)
class PhiExpr:
    reg: Register = attr.ib()
    node: Node = attr.ib()
    type: Type = attr.ib()
    used_phis: List['PhiExpr'] = attr.ib()
    name: Optional[str] = attr.ib(default=None)
    num_usages: int = attr.ib(default=0)
    replacement_expr: Optional['Expression'] = attr.ib(default=None)
    used_by: Optional['PhiExpr'] = attr.ib(default=None)

    def dependencies(self) -> List['Expression']:
        return []

    def get_var_name(self) -> str:
        return self.name or f'unnamed-phi({self.reg.register_name})'

    def use(self, from_phi: Optional['PhiExpr']=None) -> None:
        if self.num_usages == 0:
            self.used_phis.append(self)
        self.num_usages += 1
        self.used_by = from_phi

    def propagates_to(self) -> 'PhiExpr':
        if self.num_usages != 1 or self.used_by is None:
            return self
        return self.used_by.propagates_to()

    def __str__(self) -> str:
        if self.replacement_expr:
            return str(self.replacement_expr)
        return self.get_var_name()

@attr.s
class EvalOnceStmt:
    expr: EvalOnceExpr = attr.ib()

    def need_decl(self) -> bool:
        return self.expr.num_usages > 1

    def should_write(self) -> bool:
        if self.expr.always_emit:
            return self.expr.num_usages != 1
        else:
            return self.expr.num_usages > 1

    def __str__(self) -> str:
        if self.expr.always_emit and self.expr.num_usages == 0:
            return f'{self.expr.wrapped_expr};'
        return f'{self.expr.get_var_name()} = {self.expr.wrapped_expr};'

@attr.s
class SetPhiStmt:
    phi: PhiExpr = attr.ib()
    expr: 'Expression' = attr.ib()

    def should_write(self) -> bool:
        expr = self.expr
        if isinstance(expr, PhiExpr) and expr.propagates_to() != expr:
            assert expr.propagates_to() == self.phi.propagates_to()
            return False
        return True

    def __str__(self) -> str:
        return f'{self.phi.propagates_to().get_var_name()} = {self.expr};'

@attr.s
class StoreStmt:
    source: 'Expression' = attr.ib()
    dest: 'Expression' = attr.ib()

    def should_write(self) -> bool:
        return True

    def __str__(self) -> str:
        return f'{self.dest} = {self.source};'

@attr.s
class CommentStmt:
    contents: str = attr.ib()

    def should_write(self) -> bool:
        return True

    def __str__(self) -> str:
        return f'// {self.contents}'

Expression = Union[
    BinaryOp,
    UnaryOp,
    Cast,
    FuncCall,
    GlobalSymbol,
    Literal,
    AddressOf,
    LocalVar,
    PassedInArg,
    StructAccess,
    SubroutineArg,
    EvalOnceExpr,
    PhiExpr,
]

Statement = Union[
    StoreStmt,
    EvalOnceStmt,
    SetPhiStmt,
    CommentStmt,
]

@attr.s
class RegInfo:
    contents: Dict[Register, Expression] = attr.ib()
    stack_info: StackInfo = attr.ib(repr=False)
    written_in_block: Set[Register] = attr.ib(factory=set)

    def __getitem__(self, key: Register) -> Expression:
        if key == Register('zero'):
            return Literal(0)
        ret = self.get_raw(key)
        assert ret is not None, f'Read from unset register {key}'
        if isinstance(ret, PassedInArg) and not ret.copied:
            # Create a new argument object to better distinguish arguments we
            # are called with from arguments passed to subroutines. Also, unify
            # the argument's type with what we can guess from the register used.
            arg = self.stack_info.get_argument(ret.value)
            self.stack_info.add_argument(arg)
            arg.type.unify(ret.type)
            return arg
        return ret

    def __contains__(self, key: Register) -> bool:
        return key in self.contents

    def __setitem__(self, key: Register, value: Optional[Expression]) -> None:
        assert key != Register('zero')
        if value is not None:
            self.contents[key] = value
        elif key in self.contents:
            del self.contents[key]
        if key.register_name in ['f0', 'v0']:
            self[Register('return_reg')] = value
        self.written_in_block.add(key)

    def __delitem__(self, key: Register) -> None:
        assert key != Register('zero')
        del self.contents[key]

    def get_raw(self, key: Register) -> Optional[Expression]:
        return self.contents.get(key, None)

    def clear_caller_save_regs(self) -> None:
        for reg in CALLER_SAVE_REGS:
            assert reg != Register('zero')
            if reg in self.contents:
                del self.contents[reg]

    def __str__(self) -> str:
        return ', '.join(f"{k}: {v}" for k,v in sorted(self.contents.items()))


@attr.s
class BlockInfo:
    """
    Contains translated assembly code (to_write), the block's branch condition,
    and block's final register states.
    """
    to_write: List[Statement] = attr.ib()
    return_value: Optional[Expression] = attr.ib()
    branch_condition: Optional[BinaryOp] = attr.ib()
    final_register_states: RegInfo = attr.ib()

    def __str__(self) -> str:
        newline = '\n\t'
        return '\n'.join([
            f'Statements: {newline.join(str(w) for w in self.to_write if w.should_write())}',
            f'Branch condition: {self.branch_condition}',
            f'Final register states: {self.final_register_states}'])


@attr.s
class InstrArgs:
    raw_args: List[Argument] = attr.ib()
    regs: RegInfo = attr.ib(repr=False)
    stack_info: StackInfo = attr.ib(repr=False)

    def duplicate_dest_reg(self) -> None:
        self.raw_args.insert(1, self.raw_args[0])

    def reg_ref(self, index: int) -> Register:
        ret = self.raw_args[index]
        assert isinstance(ret, Register)
        return ret

    def reg(self, index: int) -> Expression:
        return self.regs[self.reg_ref(index)]

    def dreg(self, index: int) -> Expression:
        """Extract a double from a register. This may involve reading both the
        mentioned register and the next."""
        reg = self.reg_ref(index)
        assert reg.is_float()
        ret = self.regs[reg]
        if not isinstance(ret, Literal) or ret.type.get_size() == 64:
            return ret
        reg_num = int(reg.register_name[1:])
        assert reg_num % 2 == 0
        other = self.regs[Register(f'f{reg_num+1}')]
        assert isinstance(other, Literal) and other.type.get_size() != 64
        value = ret.value | (other.value << 32)
        return Literal(value, type=Type.f64())

    def imm(self, index: int) -> Expression:
        arg = strip_macros(self.raw_args[index])
        ret = literal_expr(arg, self.stack_info)
        if isinstance(ret, GlobalSymbol):
            return AddressOf(ret)
        return ret

    def hi_imm(self, index: int) -> Expression:
        arg = self.raw_args[index]
        assert isinstance(arg, Macro) and arg.macro_name == 'hi'
        ret = literal_expr(arg.argument, self.stack_info)
        if isinstance(ret, GlobalSymbol):
            return AddressOf(ret)
        return ret

    def memory_ref(self, index: int) -> Union[AddressMode, GlobalSymbol]:
        ret = strip_macros(self.raw_args[index])
        if isinstance(ret, AsmAddressMode):
            if ret.lhs is None:
                return AddressMode(offset=0, rhs=ret.rhs)
            assert isinstance(ret.lhs, AsmLiteral)  # macros were removed
            return AddressMode(offset=ret.lhs.value, rhs=ret.rhs)
        assert isinstance(ret, AsmGlobalSymbol)
        return self.stack_info.global_symbol(ret)

    def count(self) -> int:
        return len(self.raw_args)


def deref(
    arg: Union[AddressMode, GlobalSymbol],
    regs: RegInfo,
    stack_info: StackInfo,
    store: bool=False
) -> Expression:
    if isinstance(arg, AddressMode):
        location=arg.offset
        if arg.rhs.register_name in ['sp', 'fp']:
            return stack_info.get_stack_var(location, store=store)
        else:
            # Struct member is being dereferenced.
            var = regs[arg.rhs]
            var.type.unify(Type.ptr())
            return StructAccess(struct_var=var, offset=location,
                    type=stack_info.unique_type_for('struct', (var, location)))
    else:
        # Keep GlobalSymbol's as-is.
        assert isinstance(arg, GlobalSymbol)
        return arg

def is_repeatable_expression(expr: Expression) -> bool:
    # Determine whether an expression should be evaluated only once or not.
    # TODO: Some of this logic is sketchy, saying that it's fine to repeat e.g.
    # reads even though there might have been e.g. sets or function calls in
    # between. It should really take into account what has changed since the
    # expression was created and when it's used. For now, though, we make this
    # naive guess at the creation. (Another signal we could potentially use is
    # whether the expression is stored in a callee-save register.)
    if expr is None or isinstance(expr, (EvalOnceExpr, Literal, GlobalSymbol,
            LocalVar, PassedInArg, SubroutineArg)):
        return True
    if isinstance(expr, AddressOf):
        return is_repeatable_expression(expr.expr)
    if isinstance(expr, StructAccess):
        return is_repeatable_expression(expr.struct_var)
    return False

def is_type_obvious(expr: Expression) -> bool:
    """
    Determine whether an expression's is "obvious", e.g. because the expression
    refers to a variable which has a declaration. With perfect type information
    this function would not be needed.

    This function may produce wrong results while code is being generated,
    since at that point we don't know the final status of EvalOnceExpr's.
    """
    if isinstance(expr, (Cast, Literal, AddressOf, LocalVar, PassedInArg)):
        return True
    if isinstance(expr, EvalOnceExpr):
        if expr.num_usages > 1:
            return True
        return is_type_obvious(expr.wrapped_expr)
    return False

def simplify_condition(expr: Expression) -> Expression:
    """
    Simplify a boolean expression.

    This function may produce wrong results while code is being generated,
    since at that point we don't know the final status of EvalOnceExpr's.
    """
    if isinstance(expr, EvalOnceExpr) and expr.num_usages <= 1:
        return simplify_condition(expr.wrapped_expr)
    if isinstance(expr, BinaryOp):
        left = simplify_condition(expr.left)
        right = simplify_condition(expr.right)
        if (isinstance(left, BinaryOp) and left.is_boolean() and
                right == Literal(0)):
            if expr.op == '==':
                return simplify_condition(left.negated())
            if expr.op == '!=':
                return left
        return BinaryOp(left=left, op=expr.op, right=right, type=expr.type)
    return expr

def mark_used(expr: Expression) -> None:
    if isinstance(expr, (PhiExpr, EvalOnceExpr)):
        expr.use()
    else:
        for sub_expr in expr.dependencies():
            mark_used(sub_expr)

def literal_expr(arg: Argument, stack_info: StackInfo) -> Expression:
    if isinstance(arg, AsmGlobalSymbol):
        return stack_info.global_symbol(arg)
    if isinstance(arg, AsmLiteral):
        return Literal(arg.value)
    assert isinstance(arg, BinOp), f'argument {arg} must be a literal'
    return BinaryOp.int(left=literal_expr(arg.lhs, stack_info), op=arg.op,
            right=literal_expr(arg.rhs, stack_info))


def load_upper(args: InstrArgs) -> Expression:
    if isinstance(args.raw_args[1], Macro):
        return args.hi_imm(1)
    expr = args.imm(1)
    if isinstance(expr, BinaryOp) and expr.op == '>>':
        # Something like "lui REG (lhs >> 16)". Just take "lhs".
        assert expr.right == Literal(16)
        return expr.left
    else:
        assert isinstance(expr, Literal)
        # Something like "lui 0x1", meaning 0x10000.
        return Literal(expr.value << 16)

def handle_ori(args: InstrArgs) -> Expression:
    # Two-argument form, mostly used for "ori $reg, (x & 0xffff)"
    if args.count() == 2:
        args.duplicate_dest_reg()

    imm = args.imm(2)
    if isinstance(imm, BinaryOp) and imm.op == '&':
        # Something like "ori REG (lhs & 0xFFFF)". We (hopefully) already
        # handled this in the lui, but let's put lhs into this register too.
        assert imm.right == Literal(0xFFFF)
        return imm.left
    else:
        # Regular bitwise OR.
        return BinaryOp.int(left=args.reg(1), op='|', right=imm)

def handle_addi(args: InstrArgs) -> Expression:
    # Two-argument form, mostly used for "addiu $reg, %lo(...)"
    if args.count() == 2:
        args.duplicate_dest_reg()

    stack_info = args.stack_info
    source_reg = args.reg_ref(1)
    source = args.reg(1)
    imm = args.imm(2)
    if source_reg.register_name == 'zero':
        # addiu $reg, $zero, <imm> is one way of writing 'li'
        return imm
    elif imm == Literal(0):
        # addiu $reg1, $reg2, 0 is a move
        return source
    elif source_reg.register_name in ['sp', 'fp']:
        # Adding to sp, i.e. passing an address.
        assert isinstance(imm, Literal)
        if args.reg_ref(0).register_name in ['sp', 'fp']:
            # Changing sp. Just ignore that.
            return source
        # Keep track of all local variables that we take addresses of.
        var = stack_info.get_stack_var(imm.value, store=False)
        if isinstance(var, LocalVar):
            stack_info.add_local_var(var)
        return AddressOf(var)
    else:
        # Regular binary addition.
        return BinaryOp.intptr(left=source, op='+', right=imm)

def handle_load(args: InstrArgs) -> Expression:
    return deref(args.memory_ref(1), args.regs, args.stack_info)

def make_store(args: InstrArgs, type: Type) -> Optional[StoreStmt]:
    stack_info = args.stack_info
    source_reg = args.reg_ref(0)
    source_val = args.reg(0)
    target = args.memory_ref(1)
    preserve_regs = CALLEE_SAVE_REGS + ARGUMENT_REGS + SPECIAL_REGS
    if (source_reg in preserve_regs and
            isinstance(target, AddressMode) and
            target.rhs.register_name in ['sp', 'fp']):
        # Elide register preserval. TODO: This isn't really right, what if
        # we're actually using the registers...
        return None
    dest = deref(target, args.regs, stack_info, store=True)
    dest.type.unify(type)
    return StoreStmt(source=as_type(source_val, type, silent=False), dest=dest)

def parse_f32_imm(num: int) -> float:
    rep =  f'{num:032b}'  # zero-padded binary representation of num
    sign = [1, -1][int(rep[0], 2)]
    expo = int(rep[1:9], 2)
    frac = int(rep[9:], 2)
    if expo == 0:
        return float(sign * (2 ** (1 - 127)) * (frac / (2 ** 23)))
    return float(sign * (2 ** (expo - 127)) * (frac / (2 ** 23) + 1))

def parse_f64_imm(num: int) -> float:
    rep =  f'{num:064b}'  # zero-padded binary representation of num
    sign = [1, -1][int(rep[0], 2)]
    expo = int(rep[1:12], 2)
    frac = int(rep[12:], 2)
    if expo == 0:
        return float(sign * (2 ** (1 - 1023)) * (frac / (2 ** 52)))
    return float(sign * (2 ** (expo - 1023)) * (frac / (2 ** 52) + 1))

def fold_mul_chains(expr: Expression) -> Expression:
    def fold(expr: Expression, toplevel: bool) -> Tuple[Expression, int]:
        if isinstance(expr, BinaryOp):
            lbase, lnum = fold(expr.left, False)
            rbase, rnum = fold(expr.right, False)
            if expr.op == '<<' and isinstance(expr.right, Literal):
                # Left-shifts by small numbers are easier to understand if
                # written as multiplications (they compile to the same thing).
                if toplevel and lnum == 1 and not (1 <= expr.right.value <= 4):
                    return (expr, 1)
                return (lbase, lnum << expr.right.value)
            if expr.op == '*' and isinstance(expr.right, Literal):
                return (lbase, lnum * expr.right.value)
            if expr.op == '+' and lbase == rbase:
                return (lbase, lnum + rnum)
            if expr.op == '-' and lbase == rbase:
                return (lbase, lnum - rnum)
        if isinstance(expr, UnaryOp) and not toplevel:
            base, num = fold(expr.expr, False)
            return (base, -num)
        if isinstance(expr, EvalOnceExpr):
            base, num = fold(expr.wrapped_expr, False)
            if num != 1 and is_repeatable_expression(base):
                return (base, num)
        return (expr, 1)

    base, num = fold(expr, True)
    if num == 1:
        return expr
    return BinaryOp.int(left=base, op='*', right=Literal(num))

def strip_macros(arg: Argument) -> Argument:
    """Replace %lo(...) by 0, and assert that there are no %hi(...). We assume
    that %hi's only ever occur in lui, where we expand them to an entire value,
    and not just the upper part. This ought to preserve semantics in all
    reasonable cases."""
    if isinstance(arg, Macro):
        assert arg.macro_name == 'lo'
        return AsmLiteral(0)
    elif isinstance(arg, AsmAddressMode) and isinstance(arg.lhs, Macro):
        assert arg.lhs.macro_name == 'lo'
        return AsmAddressMode(lhs=None, rhs=arg.rhs)
    else:
        return arg


InstrMap = Dict[str, Callable[[InstrArgs], Expression]]
CmpInstrMap = Dict[str, Callable[[InstrArgs], Optional[BinaryOp]]]
StoreInstrMap = Dict[str, Callable[[InstrArgs], Optional[StoreStmt]]]
MaybeInstrMap = Dict[str, Callable[[InstrArgs], Optional[Expression]]]
PairInstrMap = Dict[str, Callable[[InstrArgs], Tuple[Optional[Expression], Optional[Expression]]]]

CASES_SOURCE_FIRST_EXPRESSION: StoreInstrMap = {
    # Storage instructions
    'sb': lambda a: make_store(a, type=Type.of_size(8)),
    'sh': lambda a: make_store(a, type=Type.of_size(16)),
    'sw': lambda a: make_store(a, type=Type.of_size(32)),
    # Floating point storage/conversion
    'swc1': lambda a: make_store(a, type=Type.f32()),
    'sdc1': lambda a: make_store(a, type=Type.f64()),
}
CASES_SOURCE_FIRST_REGISTER: InstrMap = {
    # Floating point moving instruction
    'mtc1': lambda a: a.reg(0),
    'ctc1': lambda a: a.reg(0),
}
CASES_BRANCHES: CmpInstrMap = {
    # Branch instructions/pseudoinstructions
    # TODO! These are wrong. (Are they??)
    'b': lambda a: None,
    'beq': lambda a:  BinaryOp.icmp(a.reg(0), '==', a.reg(1)),
    'bne': lambda a:  BinaryOp.icmp(a.reg(0), '!=', a.reg(1)),
    'beqz': lambda a: BinaryOp.icmp(a.reg(0), '==', Literal(0)),
    'bnez': lambda a: BinaryOp.icmp(a.reg(0), '!=', Literal(0)),
    'blez': lambda a: BinaryOp.icmp(a.reg(0), '<=', Literal(0)),
    'bgtz': lambda a: BinaryOp.icmp(a.reg(0), '>',  Literal(0)),
    'bltz': lambda a: BinaryOp.icmp(a.reg(0), '<',  Literal(0)),
    'bgez': lambda a: BinaryOp.icmp(a.reg(0), '>=', Literal(0)),
}
CASES_FLOAT_BRANCHES: CmpInstrMap = {
    # Floating-point branch instructions
    # We don't have to do any work here, since the condition bit was already set.
    'bc1t': lambda a: None,
    'bc1f': lambda a: None,
}
CASES_JUMPS: MaybeInstrMap = {
    # Unconditional jumps
    'jal': lambda a: a.imm(0),  # not sure what arguments!
    'jr':  lambda a: None       # not sure what to return!
}
CASES_FLOAT_COMP: CmpInstrMap = {
    # Floating point comparisons
    'c.eq.s': lambda a: BinaryOp.fcmp(a.reg(0), '==', a.reg(1)),
    'c.le.s': lambda a: BinaryOp.fcmp(a.reg(0), '<=', a.reg(1)),
    'c.lt.s': lambda a: BinaryOp.fcmp(a.reg(0), '<',  a.reg(1)),
    'c.eq.d': lambda a: BinaryOp.dcmp(a.reg(0), '==', a.reg(1)),
    'c.le.d': lambda a: BinaryOp.dcmp(a.reg(0), '<=', a.reg(1)),
    'c.lt.d': lambda a: BinaryOp.dcmp(a.reg(0), '<',  a.reg(1)),
}
CASES_HI_LO: PairInstrMap = {
    # Div and mul output two results, to LO/HI registers. (Format: (hi, lo))
    'div': lambda a: (BinaryOp.s32(a.reg(1), '%', a.reg(2)),
                      BinaryOp.s32(a.reg(1), '/', a.reg(2))),
    'divu': lambda a: (BinaryOp.u32(a.reg(1), '%', a.reg(2)),
                       BinaryOp.u32(a.reg(1), '/', a.reg(2))),
    # The high part of multiplication cannot be directly represented in C
    'multu': lambda a: (None,
                        BinaryOp.int(a.reg(0), '*', a.reg(1))),
}
CASES_DESTINATION_FIRST: InstrMap = {
    # Flag-setting instructions
    'slt': lambda a:  BinaryOp.icmp(a.reg(1), '<', a.reg(2)),
    'slti': lambda a: BinaryOp.icmp(a.reg(1), '<', a.imm(2)),
    'sltu': lambda a:  BinaryOp.ucmp(a.reg(1), '<', a.reg(2)),
    'sltiu': lambda a: BinaryOp.ucmp(a.reg(1), '<', a.imm(2)),
    # Integer arithmetic
    'addi': lambda a: handle_addi(a),
    'addiu': lambda a: handle_addi(a),
    'addu': lambda a: fold_mul_chains(BinaryOp.intptr(a.reg(1), '+', a.reg(2))),
    'subu': lambda a: fold_mul_chains(BinaryOp.intptr(a.reg(1), '-', a.reg(2))),
    'negu': lambda a: fold_mul_chains(UnaryOp(op='-',
                                expr=as_s32(a.reg(1)), type=Type.s32())),
    # Hi/lo register uses (used after division/multiplication)
    'mfhi': lambda a: a.regs[Register('hi')],
    'mflo': lambda a: a.regs[Register('lo')],
    # Floating point arithmetic
    'add.s': lambda a: BinaryOp.f32(a.reg(1), '+', a.reg(2)),
    'sub.s': lambda a: BinaryOp.f32(a.reg(1), '-', a.reg(2)),
    'neg.s': lambda a: UnaryOp('-', as_f32(a.reg(1)), type=Type.f32()),
    'div.s': lambda a: BinaryOp.f32(a.reg(1), '/', a.reg(2)),
    'mul.s': lambda a: BinaryOp.f32(a.reg(1), '*', a.reg(2)),
    # Double-precision arithmetic
    'add.d': lambda a: BinaryOp.f64(a.dreg(1), '+', a.dreg(2)),
    'sub.d': lambda a: BinaryOp.f64(a.dreg(1), '-', a.dreg(2)),
    'neg.d': lambda a: UnaryOp('-', as_f64(a.dreg(1)), type=Type.f64()),
    'div.d': lambda a: BinaryOp.f64(a.dreg(1), '/', a.dreg(2)),
    'mul.d': lambda a: BinaryOp.f64(a.dreg(1), '*', a.dreg(2)),
    # Floating point conversions
    'cvt.d.s': lambda a: Cast(expr=as_f32(a.reg(1)), type=Type.f64()),
    'cvt.d.w': lambda a: Cast(expr=as_intish(a.reg(1)), type=Type.f64()),
    'cvt.s.d': lambda a: Cast(expr=as_f64(a.dreg(1)), type=Type.f32()),
    'cvt.s.u': lambda a: Cast(expr=as_u32(a.reg(1)), type=Type.f32()),
    'cvt.s.w': lambda a: Cast(expr=as_intish(a.reg(1)), type=Type.f32()),
    'cvt.w.d': lambda a: Cast(expr=as_f64(a.dreg(1)), type=Type.s32()),
    'cvt.w.s': lambda a: Cast(expr=as_f32(a.reg(1)), type=Type.s32()),
    'trunc.w.s': lambda a: Cast(expr=as_f32(a.reg(1)), type=Type.s32()),
    'trunc.w.d': lambda a: Cast(expr=as_f64(a.dreg(1)), type=Type.s32()),
    # Bit arithmetic
    'ori':  lambda a: handle_ori(a),
    'and': lambda a: BinaryOp.int(left=a.reg(1), op='&', right=a.reg(2)),
    'or': lambda a:  BinaryOp.int(left=a.reg(1), op='|', right=a.reg(2)),
    'xor': lambda a: BinaryOp.int(left=a.reg(1), op='^', right=a.reg(2)),
    'andi': lambda a: BinaryOp.int(left=a.reg(1), op='&',  right=a.imm(2)),
    'xori': lambda a: BinaryOp.int(left=a.reg(1), op='^',  right=a.imm(2)),
    'sll': lambda a: fold_mul_chains(
                      BinaryOp.int(left=a.reg(1), op='<<', right=a.imm(2))),
    'sllv': lambda a: BinaryOp.int(left=a.reg(1), op='<<', right=a.reg(2)),
    'srl': lambda a:  BinaryOp(left=as_u32(a.reg(1)), op='>>',
                            right=as_intish(a.imm(2)), type=Type.u32()),
    'srlv': lambda a: BinaryOp(left=as_u32(a.reg(1)), op='>>',
                            right=as_intish(a.reg(2)), type=Type.u32()),
    'sra': lambda a:  BinaryOp(left=as_s32(a.reg(1)), op='>>',
                            right=as_intish(a.imm(2)), type=Type.s32()),
    'srav': lambda a: BinaryOp(left=as_s32(a.reg(1)), op='>>',
                            right=as_intish(a.reg(2)), type=Type.s32()),
    # Move pseudoinstruction
    'move': lambda a: a.reg(1),
    # Floating point moving instructions
    'mfc1': lambda a: a.reg(1),
    'cfc1': lambda a: a.reg(1),
    'mov.s': lambda a: a.reg(1),
    # (I don't know why this typing.cast is needed... mypy bug?)
    'mov.d': lambda a: typing.cast(Expression, as_f64(a.dreg(1))),
    # Loading instructions (TODO: type annotations)
    'li': lambda a: a.imm(1),
    'lui': lambda a: load_upper(a),
    'lb': lambda a: handle_load(a),
    'lh': lambda a: handle_load(a),
    'lw': lambda a: handle_load(a),
    'lbu': lambda a: handle_load(a),
    'lhu': lambda a: handle_load(a),
    'lwu': lambda a: handle_load(a),
    # Floating point loading instructions
    'lwc1': lambda a: handle_load(a),
    'ldc1': lambda a: handle_load(a),
}

def output_regs_for_instr(instr: Instruction) -> List[Register]:
    def reg_at(index: int) -> Register:
        ret = instr.args[index]
        assert isinstance(ret, Register)
        return ret

    mnemonic = instr.mnemonic
    if (mnemonic in ['nop', 'jr'] or
            mnemonic in CASES_SOURCE_FIRST_EXPRESSION or
            mnemonic in CASES_BRANCHES or
            mnemonic in CASES_FLOAT_BRANCHES):
        return []
    if mnemonic == 'jal':
        return list(map(Register, ['return_reg', 'f0', 'v0', 'v1']))
    if mnemonic in CASES_SOURCE_FIRST_REGISTER:
        return [reg_at(1)]
    if mnemonic in CASES_DESTINATION_FIRST:
        return [reg_at(0)]
    if mnemonic in CASES_FLOAT_COMP:
        return [Register('condition_bit')]
    if mnemonic in CASES_HI_LO:
        return [Register('hi'), Register('lo')]
    assert False, f"I don't know how to handle {mnemonic}!"

def regs_clobbered_until_dominator(node: Node) -> Set[Register]:
    if node.immediate_dominator is None:
        return set()
    seen = set([node.immediate_dominator])
    stack = node.parents[:]
    clobbered = set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for instr in n.block.instructions:
            clobbered.update(output_regs_for_instr(instr))
            if instr.mnemonic == 'jal':
                clobbered.update(CALLER_SAVE_REGS)
        stack.extend(n.parents)
    return clobbered

def reg_always_set(node: Node, reg: Register, dom_set: bool) -> bool:
    if node.immediate_dominator is None:
        return False
    seen = set([node.immediate_dominator])
    stack = node.parents[:]
    while stack:
        n = stack.pop()
        if n == node.immediate_dominator and not dom_set:
            return False
        if n in seen:
            continue
        seen.add(n)
        clobbered: Optional[bool] = None
        for instr in n.block.instructions:
            if instr.mnemonic == 'jal' and reg in CALLER_SAVE_REGS:
                clobbered = True
            if reg in output_regs_for_instr(instr):
                clobbered = False
        if clobbered == True:
            return False
        if clobbered is None:
            stack.extend(n.parents)
    return True

def assign_phis(used_phis: List[PhiExpr], stack_info: StackInfo) -> None:
    i = 0
    # Iterate over used phis until there are no more remaining. New ones may
    # appear during iteration, hence the while loop.
    while i < len(used_phis):
        phi = used_phis[i]
        assert phi.num_usages > 0
        assert phi.node.parents
        exprs = []
        for node in phi.node.parents:
            block_info = node.block.block_info
            assert isinstance(block_info, BlockInfo)
            exprs.append(block_info.final_register_states[phi.reg])

        if all(e == exprs[0] for e in exprs[1:]):
            # All the phis have the same value (e.g. because we recomputed an
            # expression after a store, or restored a register after a function
            # call). Just use that value instead of introducing a phi node.
            phi.replacement_expr = exprs[0]
            for _ in range(phi.num_usages):
                mark_used(exprs[0])
        else:
            for node in phi.node.parents:
                block_info = node.block.block_info
                assert isinstance(block_info, BlockInfo)
                expr = block_info.final_register_states[phi.reg]
                if isinstance(expr, PhiExpr):
                    # Explicitly mark how the expression is used if it's a phi,
                    # so we can propagate phi sets (to get rid of temporaries).
                    expr.use(from_phi=phi)
                else:
                    mark_used(expr)
                block_info.to_write.append(SetPhiStmt(phi, expr))
        i += 1

    name_counter: Dict[Register, int] = {}
    for phi in used_phis:
        if not phi.replacement_expr and phi.propagates_to() == phi:
            counter = name_counter.get(phi.reg, 0) + 1
            name_counter[phi.reg] = counter
            prefix = f'phi_{phi.reg.register_name}'
            phi.name = f'{prefix}_{counter}' if counter > 1 else prefix
            stack_info.phi_vars.append(phi)

def translate_node_body(
    node: Node, regs: RegInfo, stack_info: StackInfo
) -> BlockInfo:
    """
    Given a node and current register contents, return a BlockInfo containing
    the translated AST for that node.
    """

    to_write: List[Union[Statement]] = []
    local_var_writes: Dict[LocalVar, Tuple[Register, Expression]] = {}
    subroutine_args: List[Tuple[Expression, SubroutineArg]] = []
    branch_condition: Optional[BinaryOp] = None
    return_value: Optional[Expression] = None

    def eval_once(expr: Expression, always_emit: bool, prefix: str) -> Expression:
        if always_emit:
            # (otherwise this will be marked used once num_usages reaches 1)
            mark_used(expr)
        expr = EvalOnceExpr(wrapped_expr=expr, always_emit=always_emit,
                var=stack_info.temp_var_generator(prefix), type=expr.type)
        stmt = EvalOnceStmt(expr)
        to_write.append(stmt)
        stack_info.temp_vars.append(stmt)
        return expr

    def set_reg(reg: Register, expr: Optional[Expression]) -> None:
        if isinstance(expr, LocalVar) and expr in local_var_writes:
            # Elide register restores (only for the same register for now, to
            # be conversative).
            orig_reg, orig_expr = local_var_writes[expr]
            if orig_reg == reg:
                expr = orig_expr
        if expr is not None and not is_repeatable_expression(expr):
            expr = eval_once(expr, always_emit=False, prefix=reg.register_name)
        regs[reg] = expr

    for instr in node.block.instructions:
        # Save the current mnemonic.
        mnemonic = instr.mnemonic
        if mnemonic == 'nop':
            continue

        args = InstrArgs(instr.args, regs, stack_info)

        # Figure out what code to generate!
        if mnemonic in CASES_SOURCE_FIRST_EXPRESSION:
            # Store a value in a permanent place.
            to_store = CASES_SOURCE_FIRST_EXPRESSION[mnemonic](args)
            if to_store is not None and isinstance(to_store.dest, SubroutineArg):
                # About to call a subroutine with this argument.
                subroutine_args.append((to_store.source, to_store.dest))
            elif to_store is not None:
                if isinstance(to_store.dest, LocalVar):
                    stack_info.add_local_var(to_store.dest)
                    assert isinstance(to_store.source, Cast)
                    assert to_store.source.reinterpret
                    local_var_writes[to_store.dest] = (args.reg_ref(0),
                            to_store.source.expr)
                # This needs to be written out.
                to_write.append(to_store)
                mark_used(to_store.source)
                mark_used(to_store.dest)

        elif mnemonic in CASES_SOURCE_FIRST_REGISTER:
            # Just 'mtc1'. It's reversed, so we have to specially handle it.
            set_reg(args.reg_ref(1), CASES_SOURCE_FIRST_REGISTER[mnemonic](args))

        elif mnemonic in CASES_BRANCHES:
            assert branch_condition is None
            branch_condition = CASES_BRANCHES[mnemonic](args)

        elif mnemonic in CASES_FLOAT_BRANCHES:
            assert branch_condition is None
            cond_bit = regs[Register('condition_bit')]
            assert isinstance(cond_bit, BinaryOp)
            if mnemonic == 'bc1t':
                branch_condition = cond_bit
            elif mnemonic == 'bc1f':
                branch_condition = cond_bit.negated()

        elif mnemonic in CASES_JUMPS:
            result = CASES_JUMPS[mnemonic](args)
            if result is None:
                # Return from the function.
                assert mnemonic == 'jr'
                assert args.reg_ref(0) == Register('ra'), "Jump tables are not supported yet."
                assert isinstance(node, ReturnNode)
                return_value = regs.get_raw(Register('return_reg'))
                break
            else:
                # Function call. Well, let's double-check:
                assert mnemonic == 'jal'
                target = args.imm(0)
                assert isinstance(target, AddressOf)
                target = target.expr
                assert isinstance(target, GlobalSymbol)
                # At most one of $f12 and $a0 may be passed, and at most one of
                # $f14 and $a1. We could try to figure out which ones, and cap
                # the function call at the point where a register is empty, but
                # for now we'll leave that for manual fixup.
                func_args: List[Expression] = []
                for register in map(Register, ['f12', 'f14', 'a0', 'a1', 'a2', 'a3']):
                    # The latter check verifies that the register is not just
                    # meant for us. This might give false positives for the
                    # first function call if an argument passed in the same
                    # position as we received it, but that's impossible to do
                    # anything about without access to function signatures.
                    expr = regs.get_raw(register)
                    if expr is not None and (not isinstance(expr, PassedInArg)
                            or expr.copied):
                        func_args.append(expr)
                # Add the arguments after a3.
                subroutine_args.sort(key=lambda a: a[1].value)
                for arg in subroutine_args:
                    func_args.append(arg[0])
                # Reset subroutine_args, for the next potential function call.
                subroutine_args = []

                call: Expression = FuncCall(target.symbol_name, func_args, Type.any())
                call = eval_once(call, always_emit=True, prefix='ret')
                # Clear out caller-save registers, for clarity and to ensure
                # that argument regs don't get passed into the next function.
                regs.clear_caller_save_regs()
                # We don't know what this function's return register is,
                # be it $v0, $f0, or something else, so this hack will have
                # to do. (TODO: handle it...)
                regs[Register('f0')] = Cast(expr=call, reinterpret=True,
                        silent=True, type=Type.f32())
                regs[Register('v0')] = Cast(expr=call, reinterpret=True,
                        silent=True, type=Type.intish())
                regs[Register('v1')] = as_u32(Cast(expr=call, reinterpret=True,
                        silent=False, type=Type.u64()))
                regs[Register('return_reg')] = call

        elif mnemonic in CASES_FLOAT_COMP:
            regs[Register('condition_bit')] = CASES_FLOAT_COMP[mnemonic](args)

        elif mnemonic in CASES_HI_LO:
            hi, lo = CASES_HI_LO[mnemonic](args)
            set_reg(Register('hi'), hi)
            set_reg(Register('lo'), lo)

        elif mnemonic in CASES_DESTINATION_FIRST:
            set_reg(args.reg_ref(0), CASES_DESTINATION_FIRST[mnemonic](args))

        else:
            assert False, f"I don't know how to handle {mnemonic}!"

    if return_value is not None:
        mark_used(return_value)
    elif branch_condition is not None:
        mark_used(branch_condition)
    return BlockInfo(to_write, return_value, branch_condition, regs)


def translate_graph_from_block(
    node: Node,
    regs: RegInfo,
    stack_info: StackInfo,
    used_phis: List[PhiExpr],
    options: Options
) -> None:
    """
    Given a FlowGraph node and a dictionary of register contents, give that node
    its appropriate BlockInfo (which contains the AST of its code).
    """

    if options.debug:
        print(f'\nNode in question: {node.block}')

    # Translate the given node and discover final register states.
    try:
        block_info = translate_node_body(node, regs, stack_info)
        if options.debug:
            print(block_info)
    except Exception as e:  # TODO: handle issues better
        if options.stop_on_error:
            raise e
        traceback.print_exc()
        emsg = str(e) or traceback.format_tb(sys.exc_info()[2])[-1]
        emsg = emsg.strip().split('\n')[-1].strip()
        error_stmt = CommentStmt('Error: ' + emsg)
        block_info = BlockInfo([error_stmt], None, None, regs)

    node.block.add_block_info(block_info)

    # Translate everything dominated by this node, now that we know our own
    # final register state. This will eventually reach every node.
    for child in node.immediately_dominates:
        new_contents = regs.contents.copy()
        phi_regs = regs_clobbered_until_dominator(child)
        for reg in phi_regs:
            if reg_always_set(child, reg, (reg in regs)):
                new_contents[reg] = PhiExpr(reg=reg, node=child,
                        used_phis=used_phis, type=Type.any())
            elif reg in new_contents:
                del new_contents[reg]
        new_regs = RegInfo(
            contents=new_contents,
            stack_info=stack_info,
            written_in_block=set()
        )
        translate_graph_from_block(child, new_regs, stack_info, used_phis, options)

@attr.s
class FunctionInfo:
    stack_info: StackInfo = attr.ib()
    flow_graph: FlowGraph = attr.ib()

def translate_to_ast(function: Function, options: Options) -> FunctionInfo:
    """
    Given a function, produce a FlowGraph that both contains control-flow
    information and has AST transformations for each block of code and
    branch condition.
    """
    # Initialize info about the function.
    flow_graph: FlowGraph = build_callgraph(function)
    stack_info = get_stack_info(function, flow_graph.nodes[0])

    initial_regs: Dict[Register, Expression] = {
        Register('a0'): PassedInArg(0, copied=False, type=Type.intptr()),
        Register('a1'): PassedInArg(4, copied=False, type=Type.intptr()),
        Register('a2'): PassedInArg(8, copied=False, type=Type.any()),
        Register('a3'): PassedInArg(12, copied=False, type=Type.any()),
        Register('f12'): PassedInArg(0, copied=False, type=Type.f32()),
        Register('f14'): PassedInArg(4, copied=False, type=Type.f32()),
        **{reg: stack_info.global_symbol(AsmGlobalSymbol(reg.register_name))
            for reg in CALLEE_SAVE_REGS + SPECIAL_REGS + [Register('sp')]}
    }

    if options.debug:
        print(stack_info)
        print('\nNow, we attempt to translate:')

    start_node = flow_graph.nodes[0]
    start_reg: RegInfo = RegInfo(
        contents=initial_regs,
        stack_info=stack_info,
        written_in_block=set(initial_regs.keys())
    )
    used_phis: List[PhiExpr] = []
    translate_graph_from_block(start_node, start_reg, stack_info, used_phis, options)
    assign_phis(used_phis, stack_info)
    return FunctionInfo(stack_info, flow_graph)

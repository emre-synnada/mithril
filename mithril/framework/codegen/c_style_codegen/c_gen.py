# Copyright 2022 Synnada, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ctypes
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from functools import partial

from ....backends.with_manualgrad.c_backend import CBackend
from ....backends.with_manualgrad.ggml_backend import GGMLBackend
from ....common import CGenConfig
from ....cores.c.array import PyArray
from ...common import (
    EvaluateAllType,
    EvaluateType,
    FinalCost,
    Tensor,
)
from ...logical.operator import Operator
from ...physical.model import PhysicalModel
from ..code_gen import CodeGen
from ..utils import check_repr_inequality
from . import c_ast, utils

ast_block_type = list[c_ast.Stmt] | list[c_ast.Expr] | list[c_ast.Stmt | c_ast.Expr]


class CGen(CodeGen[PyArray]):
    dynamic_links: list[str] = []

    def __init__(self, pm: PhysicalModel[PyArray]) -> None:
        super().__init__(pm)

        assert isinstance(self.pm.backend, CBackend | GGMLBackend), (
            f"Invalid backend '{self.pm.backend.backend_type}'! Must be CBackend"
            " or GGMLBackend"
        )

        # File sub-sections
        self.imports: list[c_ast.AST] = []
        self.globals: list[c_ast.AST] = []
        self.functions: list[c_ast.AST] = []

        # This will be used to store the keys of the argument of the functions
        self.backend: CBackend | GGMLBackend = self.pm.backend
        self.configs: CGenConfig = self.backend.CODEGEN_CONFIG

        # Determine struct keys
        self.struct_keys: utils.StructKeys = self.determine_struct_keys()

        # Pre-processors for customizing operator code generation
        # Maps operator keys to functions that transform (op, inputs, context)
        self.pre_processors: dict[
            str,
            Callable[
                [Operator, Sequence[str | int | float | bool | None], str],
                tuple[
                    Operator,
                    Sequence[str | int | float | bool | None],
                    list[c_ast.Stmt],
                ],
            ],
        ] = {}

        self.post_processors: dict[
            str,
            Callable[[Operator, c_ast.Expr, str], tuple[c_ast.Expr, list[c_ast.Stmt]]],
        ] = {}

    def generate_code(self, file_path: str | None = None) -> None:
        self.file_path = file_path

        self.imports.extend(self.generate_imports())
        self.functions.append(self.generate_evaluate())
        if not self.pm.inference:
            self.functions.append(self.generate_evaluate_gradients())

        # Generate functions input/output structs
        self.generate_structs()

        # Initialize global structs
        # These structs are used to store the intermediate results
        # of the model and the gradients
        self.initialize_global_structs()

        generated_code = c_ast.FILE(self.imports, self.globals, self.functions).accept(  # type: ignore
            c_ast.CStyleCodeGenerator()
        )

        if file_path is None:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".c") as tmp_file:
                self.file_path = tmp_file.name
        else:
            self.file_path = file_path

        with open(self.file_path, "w") as f:
            f.write(generated_code)

        self.code = generated_code

    def compile_code(
        self, jit: bool = False, compile_flags: list[str] | None = None
    ) -> tuple[EvaluateType[PyArray], EvaluateAllType[PyArray] | None]:
        assert not jit, "JIT is not yet supported for CBackend"
        assert self.file_path is not None, "Code has not been generated yet!"

        # For now we are only supporting .so files
        so_file_path = self.file_path.replace(".c", ".so")

        default_compile_flags = ["cc", self.file_path, "-shared", "-fPIC", "-g"]
        if compile_flags:
            default_compile_flags = compile_flags

        # Compile the code and link the dynamic links
        subprocess.check_output(
            [
                *default_compile_flags,
                f"-L{self.backend.SRC_PATH}",
                *self.dynamic_links,
                f"-Wl,-rpath,{self.backend.SRC_PATH}",
                "-o",
                so_file_path,
            ]
        )

        # If the given file path is not absolute, make it relative to the current
        # working directory
        if so_file_path[0] != "/":
            so_file_path = "./" + so_file_path

        # Load dynamic links
        for link in self.dynamic_links:
            link_path = os.path.join(self.backend.SRC_PATH, link.replace("-l", "lib"))
            if os.path.exists(link_path + ".so"):
                link_path += ".so"
            elif os.path.exists(link_path + ".dylib"):
                link_path += ".dylib"

            ctypes.CDLL(link_path)

        # We need backend subtype
        lib = ctypes.CDLL(so_file_path)

        struct_cls = self.backend.get_struct_cls()

        # Input and output structs
        class Inputs(ctypes.Structure):
            _fields_ = [
                (key, ctypes.POINTER(struct_cls))
                for key in self.struct_keys.eval_input_keys
            ]

        class Outputs(ctypes.Structure):
            _fields_ = [
                (key, ctypes.POINTER(struct_cls))
                for key in self.struct_keys.eval_output_keys
            ]

        class GradInputs(ctypes.Structure):
            _fields_ = [
                (key, ctypes.POINTER(struct_cls))
                for key in self.struct_keys.eval_grad_input_keys
            ]

        class GradOutputs(ctypes.Structure):
            _fields_ = [
                (key, ctypes.POINTER(struct_cls))
                for key in self.struct_keys.eval_grad_output_keys
            ]

        # Set the return type and argument types
        lib.evaluate.argtypes = [ctypes.POINTER(Inputs)]
        lib.evaluate.restype = Outputs

        if not self.pm.inference:
            lib.evaluate_gradients.argtypes = [ctypes.POINTER(GradInputs)]
            lib.evaluate_gradients.restype = GradOutputs

        # we need backend data types!
        # include_internals flag is used for get internal values for backpropagation
        def evaluate_wrapper(
            params: dict[str, PyArray] | None,
            data: dict[str, PyArray] | None,
            cache: dict[str, PyArray] | None = None,
            include_internals: bool = False,
        ) -> dict[str, PyArray]:
            inputs: dict[str, PyArray] = {}
            if isinstance(params, dict):
                inputs |= params
            if isinstance(data, dict):
                inputs |= data
            if isinstance(cache, dict):
                inputs |= cache

            if self.configs.ALLOCATE_INTERNALS:
                # Allocate output arrays
                for arg_key in self.struct_keys.eval_input_keys:
                    if arg_key in inputs:
                        continue
                    if self.get_tensor_shape(arg_key) is None:
                        continue
                    arr_shape = self.get_tensor_shape(arg_key)
                    inputs[arg_key] = self.backend.empty(*arr_shape)
            inputs_struct = Inputs(
                **{
                    key: ctypes.cast(
                        ctypes.byref(inputs[key].arr),
                        ctypes.POINTER(struct_cls),
                    )
                    for key in self.struct_keys.eval_input_keys
                    if key != FinalCost
                    if self.get_tensor_shape(key) is not None
                }
            )
            inputs_struct_ptr = ctypes.pointer(inputs_struct)

            output_struct = lib.evaluate(inputs_struct_ptr)

            outputs = {}
            return_keys = (
                self.struct_keys.eval_output_keys
                if include_internals
                else self.pm.output_keys
            )
            for key in return_keys:
                if key == FinalCost and not self.backend.CODEGEN_CONFIG.RETURN_OUTPUT:
                    continue
                if key != FinalCost and self.get_tensor_shape(key) is None:
                    continue
                array_ptr = getattr(output_struct, key)

                if (
                    FinalCost in self.pm.flat_graph.output_dict
                    and key == self.pm.flat_graph.output_dict[FinalCost]
                ):
                    outputs[FinalCost] = PyArray(array_ptr.contents, shape=[1])
                    outputs[key] = PyArray(array_ptr.contents, shape=[1])
                else:
                    outputs[key] = PyArray(
                        array_ptr.contents, shape=self.get_tensor_shape(key)
                    )

            return outputs

        def evaluate_gradients_wrapper(
            params: dict[str, PyArray],
            data: dict[str, PyArray] | None = None,
            output_gradients: dict[str, PyArray] | None = None,
        ) -> tuple[dict[str, PyArray], dict[str, PyArray]]:
            if data is None:
                data = {}

            if output_gradients is None and FinalCost not in self.pm._output_keys:
                raise ValueError(
                    "Requires output gradients if final loss is not attached!"
                )
            elif output_gradients is None:
                output_gradients = {FinalCost: self.backend.ones((1,))}

            gradients = {key: value for key, value in output_gradients.items()}
            if FinalCost in output_gradients:
                gradients[self.pm.flat_graph.output_dict[FinalCost]] = output_gradients[
                    FinalCost
                ]
            forward_pass = evaluate_wrapper(
                params=params,
                data=data,
                cache={},
                include_internals=self.configs.ALLOCATE_INTERNALS,
            )

            # Create gradients for all params
            if self.configs.ALLOCATE_INTERNALS:
                for key in (
                    self.pm.flat_graph.all_source_keys - self.pm.flat_graph.unused_keys
                ):
                    # In CBackend we are creating all internal gradients with zeros.
                    if self._has_grad(key) and key not in gradients:
                        arr_shape = self.get_tensor_shape(key)
                        gradients[key] = self.backend.zeros(*arr_shape)

            gradients = {
                key + utils.BACKWARD_FN_SUFFIX: value
                for key, value in gradients.items()
            }
            inputs = params | data | gradients | forward_pass
            inputs_struct = GradInputs(
                **{
                    key: ctypes.cast(
                        ctypes.byref(inputs[key].arr),
                        ctypes.POINTER(struct_cls),
                    )
                    for key in self.struct_keys.eval_grad_input_keys
                    if self.get_tensor_shape(key) is not None
                }
            )

            inputs_struct_ptr = ctypes.pointer(inputs_struct)

            output_struct = lib.evaluate_gradients(inputs_struct_ptr)
            gradients = {}
            for grad_key in self.struct_keys.eval_grad_output_keys:
                key = grad_key.replace(utils.BACKWARD_FN_SUFFIX, "")
                array_ptr = getattr(output_struct, grad_key)
                gradients[key] = PyArray(
                    array_ptr.contents, shape=self.get_tensor_shape(key)
                )

            outputs = {}
            for output_key in self.pm.output_keys:
                outputs[output_key] = forward_pass[output_key]

            return outputs, gradients

        return evaluate_wrapper, evaluate_gradients_wrapper  # type: ignore

    def generate_imports(self) -> list[c_ast.Include]:
        header_path = os.path.join(self.backend.SRC_PATH, self.configs.HEADER_NAME)
        return [c_ast.Include(header_path, system=False)]

    def generate_evaluate(self) -> c_ast.FunctionDef:
        # Function body
        pre_process: ast_block_type = []
        operations: ast_block_type = []
        post_process: ast_block_type = []

        # Define function arguments
        arguments = [
            c_ast.Parameter(
                c_ast.Pointer(f"struct {utils.EVALUATE_INPUT_STRUCT_NAME}"), "inputs"
            )
        ]

        for output_key in self.pm.flat_graph.topological_order:
            op = self.pm.flat_graph.get_op(output_key)
            inputs = self.pm.flat_graph.get_source_keys(output_key)

            # In some backends the output is used as input
            if self.configs.USE_OUTPUT_AS_INPUT:
                inputs = [output_key] + inputs

            # Create primitive call
            op_ast = self.generate_op(op, inputs, output_key, context="eval")
            operations.extend(op_ast)  # type: ignore

        # Prepare output
        post_process.append(self.create_output_struct(context="eval"))  # type: ignore
        post_process.append(c_ast.Return(c_ast.Variable("output_struct")))  # type: ignore

        evaluate_fn = self.define_function(
            f"struct {utils.EVALUATE_OUTPUT_STRUCT_NAME}",
            "evaluate",
            arguments,
            pre_process,
            operations,
            post_process,
        )

        return evaluate_fn

    def generate_evaluate_gradients(self) -> c_ast.FunctionDef:
        # Function body
        pre_process: ast_block_type = []
        operations: ast_block_type = []
        post_process: ast_block_type = []

        # Define function arguments
        arguments = [
            c_ast.Parameter(
                c_ast.Pointer(f"struct {utils.EVALUATE_GRAD_INPUT_STRUCT_NAME}"),
                "inputs",
            )
        ]

        for output_key in reversed(list(self.pm.flat_graph.topological_order)):
            # Staticly infered and unused model will not be added
            if not self._has_grad(output_key):
                continue

            op = self.pm.flat_graph.get_op(output_key)

            inputs = self.pm.flat_graph.get_source_keys(output_key)

            # Assume all inputs are Array
            for idx in range(len(inputs)):
                if not self._has_grad(inputs[idx]):
                    continue
                if (
                    FinalCost in self.pm.flat_graph.output_dict
                    and output_key == self.pm.flat_graph.output_dict[FinalCost]
                ):
                    output_key = FinalCost

                fn_inputs: list[str | int] = [
                    output_key + utils.BACKWARD_FN_SUFFIX,
                    idx,
                    output_key,
                    *inputs,
                ]

                if self.configs.USE_OUTPUT_AS_INPUT:
                    fn_inputs += [
                        input_key + utils.BACKWARD_FN_SUFFIX
                        if self._has_grad(input_key)
                        else "NULL"
                        for input_key in inputs
                        if self.pm.flat_graph.all_data[input_key].is_tensor
                    ]

                if output_key is FinalCost:
                    out_shape = self.pm.data[
                        self.pm.flat_graph.output_dict[FinalCost]
                    ].shape
                else:
                    out_shape = self.pm.data[output_key].shape

                post_process_op: (
                    Callable[
                        [Operator, c_ast.Expr, str], tuple[c_ast.Expr, list[c_ast.Stmt]]
                    ]
                    | None
                ) = None

                if (
                    (in_shape := self.pm.data[inputs[idx]].shape) is not None
                    and (out_shape) is not None
                    and check_repr_inequality(in_shape, out_shape)
                    and not self.configs.USE_OUTPUT_AS_INPUT
                ):
                    post_process_op = lambda op, op_call, context, input_key: (  # type: ignore #noqa: E731
                        c_ast.Call(
                            "accumulate_grads",
                            [
                                "eval_grad_static_ctx",
                                op_call,
                                self.create_key_ref(input_key, context="eval_grad"),
                            ],
                        ),
                        [],
                    )
                    post_process_op = partial(  # type: ignore
                        post_process_op,  # type: ignore
                        input_key=inputs[idx],
                    )

                # Create primitive call
                op_ast = self.generate_op(
                    op,
                    fn_inputs,
                    inputs[idx] + utils.BACKWARD_FN_SUFFIX,
                    context="eval_grad",
                    post_processor=post_process_op,
                )

                operations.extend(op_ast)  # type: ignore

        # Prepare output
        post_process.append(self.create_output_struct(context="eval_grad"))  # type: ignore
        post_process.append(c_ast.Return(c_ast.Variable("output_struct")))  # type: ignore

        evaluate_grad_fn = self.define_function(
            f"struct {utils.EVALUATE_GRAD_OUTPUT_STRUCT_NAME}",
            "evaluate_gradients",
            arguments,
            pre_process,
            operations,
            post_process,
        )

        return evaluate_grad_fn

    def generate_op(
        self,
        op: Operator,
        inputs: Sequence[str | int | float | bool | None],
        output_key: str,
        context: str,
        pre_processor: Callable[
            [Operator, Sequence[str | int | float | bool | None], str],
            tuple[
                Operator, Sequence[str | int | float | bool | None], list[c_ast.Stmt]
            ],
        ]
        | None = None,
        post_processor: Callable[
            [Operator, c_ast.Expr, str], tuple[c_ast.Expr, list[c_ast.Stmt]]
        ]
        | None = None,
    ) -> list[c_ast.Stmt]:
        pre_op_call: list[c_ast.Stmt] = []
        post_op_call: list[c_ast.Stmt] = []

        op, inputs, pre_lines = self.pre_process_op(op, inputs, context, pre_processor)
        pre_op_call.extend(pre_lines)

        input_vars = [
            self.create_key_ref(key, context=context, load=True)
            if isinstance(key, str)
            else c_ast.Constant(key)
            for key in inputs
        ]

        formula_key = (
            op.formula_key
            if context == "eval"
            else op.formula_key + utils.BACKWARD_FN_SUFFIX
        )

        # Create op call
        op_call = self.call_op(formula_key, input_vars, context)

        op_call, post_lines = self.post_process_op(op, op_call, context, post_processor)
        post_op_call += post_lines

        op_ast = self.assign_primitive_output(output_key, op_call, context=context)

        op_lines = pre_op_call + [op_ast] + post_op_call

        return op_lines

    def call_op(
        self, formula_key: str, input_vars: list[c_ast.Expr], context: str
    ) -> c_ast.Expr:
        return c_ast.Call(formula_key, input_vars)

    def assign_primitive_output(
        self, target: str, source: c_ast.Expr, context: str
    ) -> c_ast.Assign:
        return self.assign_array(
            self.create_key_ref(target, context=context, load=False), source
        )

    def create_key_ref(self, key: str, context: str, load: bool = True) -> c_ast.Expr:
        # TODO: This is a bit of a hack, we should have a better way to handle this
        if key in self.struct_keys.eval_cache_keys:
            if key == FinalCost and FinalCost in self.pm.flat_graph.output_dict:
                key = self.pm.flat_graph.output_dict[FinalCost]
            return c_ast.Variable(f"{utils.CACHE_NAME}.{key}")

        elif context == "eval" and key in self.struct_keys.eval_input_keys:
            return c_ast.Arrow(c_ast.Variable("inputs"), key)

        elif context == "eval_grad":
            if key in self.struct_keys.eval_grad_input_keys:
                return c_ast.Arrow(c_ast.Variable("inputs"), key)

            if (
                key in self.pm.flat_graph.all_keys
                or key.replace(utils.BACKWARD_FN_SUFFIX, "")
                in self.pm.flat_graph.all_keys
            ) and not load:
                return c_ast.Variable(f"{self.configs.ARRAY_NAME} * {key}")

        return c_ast.Variable(key)

    def assign_array(
        self, target: c_ast.Variable | c_ast.Expr, source: c_ast.Expr
    ) -> c_ast.Assign:
        return c_ast.Assign(target, source)

    def define_function(
        self,
        return_type: str,
        name: str,
        params: list[c_ast.Parameter],
        pre_process: ast_block_type,
        operations: ast_block_type,
        post_process: ast_block_type,
    ) -> c_ast.FunctionDef:
        body = pre_process + operations + post_process
        return c_ast.FunctionDef(return_type, name, params, body)

    def init_struct(
        self,
        struct_name: str,
        declaration_list: dict[str, c_ast.Expr],
        static: bool = True,
    ) -> c_ast.StructInit:
        return c_ast.StructInit(struct_name, declaration_list, static)

    def create_output_struct(self, context: str) -> c_ast.StructInit:
        output_keys = (
            self.struct_keys.eval_output_keys
            if context == "eval"
            else self.struct_keys.eval_grad_output_keys
        )
        output_struct_init: dict[str, c_ast.Expr] = {
            key: self.create_key_ref(key, context=context) for key in output_keys
        }

        output_struct_name = (
            utils.EVALUATE_OUTPUT_STRUCT_NAME
            if context == "eval"
            else utils.EVALUATE_GRAD_OUTPUT_STRUCT_NAME
        )

        return c_ast.StructInit(
            f"{output_struct_name} output_struct", output_struct_init
        )

    def generate_structs(self) -> None:
        # Generate structs
        eval_input_struct = self.generate_struct(
            utils.EVALUATE_INPUT_STRUCT_NAME,
            self.struct_keys.eval_input_keys,
        )
        eval_outputs_struct = self.generate_struct(
            utils.EVALUATE_OUTPUT_STRUCT_NAME,
            self.struct_keys.eval_output_keys,
        )

        cache_struct = self.generate_struct(
            utils.CACHE_STRUCT_NAME, self.struct_keys.eval_cache_keys
        )

        structs = [eval_input_struct, eval_outputs_struct, cache_struct]

        if not self.pm.inference:
            eval_grad_input_struct = self.generate_struct(
                utils.EVALUATE_GRAD_INPUT_STRUCT_NAME,
                self.struct_keys.eval_grad_input_keys,
            )

            eval_grad_outputs_struct = self.generate_struct(
                utils.EVALUATE_GRAD_OUTPUT_STRUCT_NAME,
                self.struct_keys.eval_grad_output_keys,
            )

            structs += [eval_grad_input_struct, eval_grad_outputs_struct]

        self.globals = structs + self.globals

    def generate_struct(self, name: str, field_keys: list[str]) -> c_ast.Stmt:
        fields = [
            c_ast.StructField(
                c_ast.Pointer(c_ast.Variable(self.configs.ARRAY_NAME)), key
            )
            for key in sorted(field_keys)
        ]
        struct = c_ast.StructDef(name, fields)
        return struct

    def determine_struct_keys(self) -> utils.StructKeys:
        struct_keys = utils.StructKeys()

        struct_keys.eval_input_keys = sorted(
            {
                key
                for key in self.pm.input_keys
                if key not in self.pm.flat_graph.data_store.data_values
                or isinstance(self.pm.data[key], Tensor)
            }
        )
        if self.configs.USE_OUTPUT_AS_INPUT:
            struct_keys.eval_input_keys = sorted(self.pm.flat_graph.all_keys)

        struct_keys.eval_output_keys = sorted(self.pm.output_keys)
        struct_keys.eval_cache_keys = sorted(
            self.pm.flat_graph.all_keys - self.pm.input_keys
        )

        struct_keys.eval_grad_input_keys = sorted(
            (
                self.pm.input_keys
                | set(self.pm.output_keys)
                | {key + utils.BACKWARD_FN_SUFFIX for key in self.pm.cotangent_keys}
            )
            - set(struct_keys.eval_cache_keys)
        )

        struct_keys.eval_grad_output_keys = sorted(
            [
                key + utils.BACKWARD_FN_SUFFIX
                for key in set(self.pm.input_keys)
                if self._has_grad(key)
            ]
        )

        return struct_keys

    def initialize_global_structs(self) -> None:
        # Init cache struct
        cache_struct = c_ast.StructInit(
            f"{utils.CACHE_STRUCT_NAME} {utils.CACHE_NAME}",
            {key: "NULL" for key in self.struct_keys.eval_cache_keys},
            static=True,
        )
        self.globals.append(cache_struct)

        if not self.pm.inference:
            # Init grad struct
            grad_struct = c_ast.StructInit(
                f"{utils.EVALUATE_GRAD_OUTPUT_STRUCT_NAME} {utils.GRAD_STRUCT_NAME}",
                {key: "NULL" for key in self.struct_keys.eval_grad_output_keys},
                static=True,
            )
            self.globals.append(grad_struct)

    def pre_process_op(
        self,
        op: Operator,
        inputs: Sequence[str | int | float | bool | None],
        context: str,
        pre_processor: Callable[
            [Operator, Sequence[str | int | float | bool | None], str],
            tuple[
                Operator, Sequence[str | int | float | bool | None], list[c_ast.Stmt]
            ],
        ]
        | None = None,
    ) -> tuple[Operator, Sequence[str | int | float | bool | None], list[c_ast.Stmt]]:
        # Default pre-processor will always be applied
        default_pre_processor = self.pre_processors.get(op.formula_key)

        pre_op_stmts: list[c_ast.Stmt] = []
        if default_pre_processor:
            op, inputs, default_pre_op_stmts = default_pre_processor(
                op, inputs, context
            )
            pre_op_stmts.extend(default_pre_op_stmts)

        if pre_processor:
            op, inputs, custom_pre_op_stmts = pre_processor(op, inputs, context)
            pre_op_stmts.extend(custom_pre_op_stmts)
        return op, inputs, pre_op_stmts

    def post_process_op(
        self,
        op: Operator,
        op_call: c_ast.Expr,
        context: str,
        post_processor: Callable[
            [Operator, c_ast.Expr, str], tuple[c_ast.Expr, list[c_ast.Stmt]]
        ]
        | None = None,
    ) -> tuple[c_ast.Expr, list[c_ast.Stmt]]:
        # Default post-processor will always be applied
        default_post_processor = self.post_processors.get(op.formula_key)

        post_op_stmts: list[c_ast.Stmt] = []
        if default_post_processor:
            op_call, default_post_op_stmts = default_post_processor(
                op, op_call, context
            )
            post_op_stmts.extend(default_post_op_stmts)

        if post_processor:
            op_call, custom_post_op_stmts = post_processor(op, op_call, context)
            post_op_stmts.extend(custom_post_op_stmts)

        return op_call, post_op_stmts

    def get_tensor_shape(self, key: str) -> tuple[int, ...]:
        if key.startswith(FinalCost):
            return (1,)
        if key in self.pm.shapes:
            return self.pm.shapes[key]  # type: ignore
        elif key.replace(utils.BACKWARD_FN_SUFFIX, "") in self.pm.shapes:
            return self.pm.shapes[key.replace(utils.BACKWARD_FN_SUFFIX, "")]  # type: ignore
        else:
            raise ValueError(f"Shape for key {key} not found")

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

import re
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO

import pytest

import mithril
from mithril import JaxBackend, NumpyBackend, TorchBackend
from mithril.framework.common import (
    TBD,
    ShapeTemplateType,
    Table,
    Tensor,
    UniadicRecord,
    Variadic,
    get_summary_shapes,
)
from mithril.models import (
    L1,
    L2,
    MLP,
    Add,
    Buffer,
    Concat,
    Connection,
    Convolution1D,
    Convolution2D,
    CrossEntropy,
    Divide,
    Flatten,
    IOKey,
    KernelizedSVM,
    LeakyRelu,
    Linear,
    MatrixMultiply,
    Max,
    MaxPool1D,
    Mean,
    Min,
    Model,
    Power,
    RBFKernel,
    Relu,
    Shape,
    Sigmoid,
    Size,
    SquaredError,
    Sum,
    Tanh,
    ToTensor,
    TrainModel,
    define_unique_names,
)

# TODO: Remove dependency to examples folder (Create a model zoo and include ResNets)!
from .resnet_model import resnet18, resnet34


def create_layer(
    out_channels, kernel_size=3, stride=1, padding=2, maxpool_kernel_size=2
):
    model = Model()
    model += Convolution1D(
        kernel_size=kernel_size,
        out_channels=out_channels,
        stride=stride,
        padding=padding,
    )
    model += Relu()
    model += MaxPool1D(kernel_size=maxpool_kernel_size)
    return model


def test_extract_logical_connections_1():
    model1 = Model()
    lin1 = Linear()
    lin2 = Linear()
    lin3 = Linear()
    model1 |= lin1.connect(input="input", weight="weight", bias="bias", output="output")
    model1 |= lin2.connect(input=lin1.output, weight=lin1.output, output="output2")
    model1 |= lin3.connect(input=lin1.weight, weight=lin1.weight, output="output3")
    model1.expose_keys("output", "output2", "output3")
    name_mappings = define_unique_names(model1.dag.keys())
    conns = model1.extract_connection_info(name_mappings)
    assert conns == {
        "Linear_0": (
            {
                "weight": ["'weight'"],
                "input": ["'input'"],
                "bias": ["'bias'"],
            },
            {"output": ["Linear_1.weight", "Linear_1.input", "'output'"]},
        ),
        "Linear_1": (
            {
                "weight": ["Linear_0.output"],
                "input": ["Linear_0.output"],
                "bias": ["'$_bias_0'"],
            },
            {"output": ["'output2'"]},
        ),
        "Linear_2": (
            {
                "weight": ["'weight'"],
                "input": ["'weight'"],
                "bias": ["'$_bias_1'"],
            },
            {"output": ["'output3'"]},
        ),
    }


def test_extract_logical_connections_2():
    model = Model()
    sig1 = Sigmoid()
    sig2 = Sigmoid()
    model |= sig1.connect(input="input1", output="output1")
    model |= sig2.connect(input="input2", output="output2")
    model.expose_keys("output1", "output2")
    model.set_cin("input1")
    model.set_cout("output1")
    buff3 = Relu()
    model2 = Model()
    model2 |= model.connect()
    model2 |= buff3.connect(input=model.output1, output=model.input2)  # type: ignore
    model2.set_cin(model.input1)  # type: ignore
    name_mappings = define_unique_names(model2.dag.keys())
    conns = model2.extract_connection_info(name_mappings)
    ref_conns = {
        "Model": (
            {"input1": ["'$input'"], "input2": ["Relu.output"]},
            {"output1": ["Relu.input"], "output2": []},
        ),
        "Relu": ({"input": ["Model.output1"]}, {"output": ["Model.input2"]}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_3():
    model = Model()
    buff1 = Buffer()
    buff2 = Buffer()

    model |= buff2.connect(output=IOKey(name="output"))
    model |= buff1.connect(output=buff2.input, input="input")
    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)
    ref_conns = {
        "Buffer_0": ({"input": ["Buffer_1.output"]}, {"output": ["'output'"]}),
        "Buffer_1": ({"input": ["'input'"]}, {"output": ["Buffer_0.input"]}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_4():
    three_out_model = Model()
    three_out_model |= Buffer().connect(input="input1", output="output1")
    three_out_model |= Buffer().connect(input="output1", output="output2")
    three_out_model |= Buffer().connect(input="input2", output="output3")
    three_out_model.expose_keys("output1", "output2", "output3")

    model = Model()

    model_1, model_2 = deepcopy(three_out_model), deepcopy(three_out_model)

    model |= model_1.connect(
        output1="out_1",
        output2="out_2",
        output3="out_3",
    )
    model.merge_connections(model_1.input1, model_1.input2)  # type: ignore
    model |= model_2.connect(
        output1=model_1.input1,  # type: ignore
        output2="out_4",
        output3="out_5",
        input1="in1",
        input2="in2",
    )
    model.expose_keys("out_1", "out_2", "out_3", "out_4", "out_5")

    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)

    ref_conns = {
        "Model_0": (
            {"input1": ["Model_1.output1"], "input2": ["Model_1.output1"]},
            {"output1": ["'out_1'"], "output2": ["'out_2'"], "output3": ["'out_3'"]},
        ),
        "Model_1": (
            {"input1": ["'in1'"], "input2": ["'in2'"]},
            {
                "output1": ["Model_0.input1", "Model_0.input2"],
                "output2": ["'out_4'"],
                "output3": ["'out_5'"],
            },
        ),
    }
    assert conns == ref_conns


def test_extract_logical_connections_5():
    model = Model()
    model += create_layer(16)
    model += create_layer(32)
    model += Flatten(start_dim=1)
    model += Linear(1000)
    model += Linear(1)

    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)

    ref_conns = {
        "Model_0": (
            {
                "$weight": ["'$weight_0'"],
                "$input": ["'$input'"],
                "$bias": ["'$bias_0'"],
            },
            {"$output": ["Model_1.$input"]},
        ),
        "Model_1": (
            {
                "$weight": ["'$weight_1'"],
                "$input": ["Model_0.$output"],
                "$bias": ["'$bias_1'"],
            },
            {"$output": ["Flatten.input"]},
        ),
        "Flatten": (
            {"input": ["Model_1.$output"], "start_dim": ["1"], "end_dim": ["-1"]},
            {"output": ["Linear_0.input"]},
        ),
        "Linear_0": (
            {
                "weight": ["'$weight_2'"],
                "input": ["Flatten.output"],
                "bias": ["'$bias_2'"],
            },
            {"output": ["Linear_1.input"]},
        ),
        "Linear_1": (
            {
                "weight": ["'$weight_3'"],
                "input": ["Linear_0.output"],
                "bias": ["'$bias_3'"],
            },
            {"output": ["'$output'"]},
        ),
    }

    assert conns == ref_conns


def test_extract_logical_connections_6():
    model = Model()
    model += Linear(dimension=3).connect(input="input", output="output")
    model += Flatten()
    model += Mean(keepdim=True)
    model.expose_keys("output")
    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)
    ref_conns = {
        "Linear": (
            {
                "weight": ["'$weight'"],
                "input": ["'input'"],
                "bias": ["'$bias'"],
            },
            {"output": ["Flatten.input", "'output'"]},
        ),
        "Flatten": (
            {"input": ["Linear.output"], "start_dim": ["0"], "end_dim": ["-1"]},
            {"output": ["Mean.input"]},
        ),
        "Mean": (
            {"input": ["Flatten.output"], "axis": ["None"], "keepdim": ["True"]},
            {"output": []},
        ),
    }
    assert conns == ref_conns


def test_extract_logical_connections_7():
    model = Model()
    model += Linear(dimension=3).connect(input="input", output="output")
    model += Sigmoid()
    model += Mean(keepdim=True)
    model.expose_keys("output")
    model_1, model_2 = deepcopy(model), deepcopy(model)
    model += model_1
    model += model_2
    model += Flatten()
    model += Buffer()
    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)

    ref_conns = {
        "Linear": (
            {
                "weight": ["'$weight_0'"],
                "input": ["'input'"],
                "bias": ["'$bias_0'"],
            },
            {"output": ["Sigmoid.input", "'output'"]},
        ),
        "Sigmoid": ({"input": ["Linear.output"]}, {"output": ["Mean.input"]}),
        "Mean": (
            {"input": ["Sigmoid.output"], "axis": ["None"], "keepdim": ["True"]},
            {"output": ["Model_0.input"]},
        ),
        "Model_0": (
            {
                "$weight": ["'$weight_1'"],
                "input": ["Mean.output"],
                "$bias": ["'$bias_1'"],
            },
            {"output": [], "$output": ["Model_1.input"]},
        ),
        "Model_1": (
            {
                "$weight": ["'$weight_2'"],
                "input": ["Model_0.$output"],
                "$bias": ["'$bias_2'"],
            },
            {"output": [], "$output": ["Flatten.input"]},
        ),
        "Flatten": (
            {"input": ["Model_1.$output"], "start_dim": ["0"], "end_dim": ["-1"]},
            {"output": ["Buffer.input"]},
        ),
        "Buffer": ({"input": ["Flatten.output"]}, {"output": []}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_8():
    model_1 = Model()
    buff_1 = Buffer()
    model_1 |= Buffer().connect(input="input", output="output1")
    model_1 |= Sigmoid().connect(input="output1", output="output2")
    model_1.expose_keys("output1", "output2")
    model_2 = Model()
    model_2 |= model_1
    model_2 += buff_1
    name_mappings = define_unique_names(model_2.dag.keys())
    conns = model_2.extract_connection_info(name_mappings)
    ref_conns = {
        "Model": (
            {"input": ["'$input'"]},
            {"output1": [], "output2": ["Buffer.input"]},
        ),
        "Buffer": ({"input": ["Model.output2"]}, {"output": ["'$output'"]}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_9():
    model_1 = Model()
    buff_1 = Buffer()
    buff_2 = Buffer()
    model_1 |= buff_1.connect(input="input", output=IOKey(name="output1"))
    model_1 |= buff_2.connect(input="output1", output=IOKey(name="output2"))
    model_n = Model()
    for model in (deepcopy(model_1) for n in range(3)):
        model_n += model
    model_nm = Model()
    for model in (deepcopy(model_n) for m in range(3)):
        model_nm += model

    name_mappings = define_unique_names(model_nm.dag.keys())
    conns = model_nm.extract_connection_info(name_mappings)
    ref_conns = {
        "Model_0": ({"$input": ["'$input'"]}, {"$output2": ["Model_1.$input"]}),
        "Model_1": ({"$input": ["Model_0.$output2"]}, {"$output2": ["Model_2.$input"]}),
        "Model_2": ({"$input": ["Model_1.$output2"]}, {"$output2": ["'$output'"]}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_10():
    model_0 = Model()
    buff_1 = Buffer()
    buff_2 = Buffer()
    buff_3 = Buffer()
    model_0 |= buff_1.connect(input="input1")
    model_0 |= buff_2.connect(input="input2")
    model_0 |= buff_3.connect(input="input3")
    model_0.set_cin("input1")
    model_0.set_cout(buff_1.output)

    model_1 = deepcopy(model_0)
    model_2 = deepcopy(model_0)
    model_3 = deepcopy(model_0)
    model = Model()
    model |= model_0
    model += model_1
    model += model_2
    model += model_3

    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)

    ref_conns = {
        "Model_0": (
            {
                "input1": ["'$input'"],
                "input2": ["'$input2_0'"],
                "input3": ["'$input3_0'"],
            },
            {"$output": ["Model_1.input1"]},
        ),
        "Model_1": (
            {
                "input1": ["Model_0.$output"],
                "input2": ["'$input2_1'"],
                "input3": ["'$input3_1'"],
            },
            {"$output": ["Model_2.input1"]},
        ),
        "Model_2": (
            {
                "input1": ["Model_1.$output"],
                "input2": ["'$input2_2'"],
                "input3": ["'$input3_2'"],
            },
            {"$output": ["Model_3.input1"]},
        ),
        "Model_3": (
            {
                "input1": ["Model_2.$output"],
                "input2": ["'$input2_3'"],
                "input3": ["'$input3_3'"],
            },
            {"$output": ["'$output'"]},
        ),
    }

    assert conns == ref_conns


def test_extract_logical_connections_11():
    model_0 = Model()
    model_0 += Buffer()
    model_1, model_2 = deepcopy(model_0), deepcopy(model_0)
    model = Model()
    model += model_0
    model += model_1
    model += model_2
    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)
    ref_conns = {
        "Model_0": ({"$input": ["'$input'"]}, {"$output": ["Model_1.$input"]}),
        "Model_1": ({"$input": ["Model_0.$output"]}, {"$output": ["Model_2.$input"]}),
        "Model_2": ({"$input": ["Model_1.$output"]}, {"$output": ["'$output'"]}),
    }
    assert conns == ref_conns


def test_extract_logical_connections_12():
    model = Model()
    model += Sigmoid()
    model_1, model_2, model_3 = tuple(deepcopy(model) for _ in range(3))
    model += model_1
    model += model_2
    model += model_3
    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)
    ref_conns = {
        "Sigmoid": ({"input": ["'$input'"]}, {"output": ["Model_0.$input"]}),
        "Model_0": ({"$input": ["Sigmoid.output"]}, {"$output": ["Model_1.$input"]}),
        "Model_1": ({"$input": ["Model_0.$output"]}, {"$output": ["Model_2.$input"]}),
        "Model_2": ({"$input": ["Model_1.$output"]}, {"$output": ["'$output'"]}),
    }

    assert conns == ref_conns


def test_extract_logical_connections_13():
    model = Model()
    model += create_layer(16)
    model += create_layer(32)
    model += Flatten(start_dim=1)
    model += Linear(1000)
    model += Linear(1)

    name_mappings = define_unique_names(model.dag.keys())
    conns = model.extract_connection_info(name_mappings)
    ref_conns = {
        "Model_0": (
            {
                "$weight": ["'$weight_0'"],
                "$input": ["'$input'"],
                "$bias": ["'$bias_0'"],
            },
            {"$output": ["Model_1.$input"]},
        ),
        "Model_1": (
            {
                "$weight": ["'$weight_1'"],
                "$input": ["Model_0.$output"],
                "$bias": ["'$bias_1'"],
            },
            {"$output": ["Flatten.input"]},
        ),
        "Flatten": (
            {"input": ["Model_1.$output"], "start_dim": ["1"], "end_dim": ["-1"]},
            {"output": ["Linear_0.input"]},
        ),
        "Linear_0": (
            {
                "weight": ["'$weight_2'"],
                "input": ["Flatten.output"],
                "bias": ["'$bias_2'"],
            },
            {"output": ["Linear_1.input"]},
        ),
        "Linear_1": (
            {
                "weight": ["'$weight_3'"],
                "input": ["Linear_0.output"],
                "bias": ["'$bias_3'"],
            },
            {"output": ["'$output'"]},
        ),
    }
    assert conns == ref_conns


def test_extract_shapes_logical_1():
    model = Model()
    buff1 = Buffer()
    buff1.set_shapes(input=[37, 23])
    buff2 = Buffer()
    model |= buff1.connect(input="input")
    model |= buff2.connect(input=buff1.output)
    name_mappings = define_unique_names(model.dag.keys())
    uni_cache: dict[UniadicRecord, str] = {}
    var_cache: dict[Variadic, str] = {}
    conn_info = model.extract_connection_info(name_mappings)
    model_shapes = {
        sub_model_name: sub_model.get_shapes(uni_cache, var_cache, False, False)
        for sub_model, sub_model_name in name_mappings.items()
    }
    shape_info = get_summary_shapes(model_shapes, conn_info)
    assert shape_info == {
        "Buffer_0": ({"input": [37, 23]}, {"output": [37, 23]}),
        "Buffer_1": ({"input": [37, 23]}, {"output": [37, 23]}),
    }


def test_extract_shapes_logical_2():
    model = Model()
    buff1 = Buffer()
    buff2 = Buffer()
    model |= buff1.connect(input="input")
    model |= buff2.connect(input=buff1.output)
    model.set_shapes(input=[45, 96, 2])
    name_mappings = define_unique_names(model.dag.keys())
    uni_cache: dict[UniadicRecord, str] = {}
    var_cache: dict[Variadic, str] = {}
    conn_info = model.extract_connection_info(name_mappings)
    model_shapes = {
        sub_model_name: sub_model.get_shapes(uni_cache, var_cache, False, False)
        for sub_model, sub_model_name in name_mappings.items()
    }
    shape_info = get_summary_shapes(model_shapes, conn_info)
    assert shape_info == {
        "Buffer_0": ({"input": [45, 96, 2]}, {"output": [45, 96, 2]}),
        "Buffer_1": ({"input": [45, 96, 2]}, {"output": [45, 96, 2]}),
    }


def test_extract_shapes_logical_3():
    model = Model()
    linear_1 = Linear(dimension=4)
    linear_2 = Linear(dimension=2)
    linear_3 = Linear(dimension=1)
    relu_1 = Relu()
    relu_2 = Relu()
    relu_3 = Relu()

    model += linear_1.connect(input="input", weight="weight", bias="bias")
    model += relu_1
    model += linear_2
    model += relu_2
    model += linear_3
    model += relu_3
    relu_2.set_shapes(input=[4, 2])
    name_mappings = define_unique_names(model.dag.keys())
    uni_cache: dict[UniadicRecord, str] = {}
    var_cache: dict[Variadic, str] = {}
    conn_info = model.extract_connection_info(name_mappings)
    model_shapes = {
        sub_model_name: sub_model.get_shapes(uni_cache, var_cache, symbolic=True)
        for sub_model, sub_model_name in name_mappings.items()
    }
    shape_info = get_summary_shapes(model_shapes, conn_info)
    assert shape_info == {
        "Linear_0": (
            {"weight": [4, "u1"], "input": [4, "u1"], "bias": [4]},
            {"output": [4, 4]},
        ),
        "Relu_0": ({"input": [4, 4]}, {"output": [4, 4]}),
        "Linear_1": (
            {"weight": [2, 4], "input": [4, 4], "bias": [2]},
            {"output": [4, 2]},
        ),
        "Relu_1": ({"input": [4, 2]}, {"output": [4, 2]}),
        "Linear_2": (
            {"weight": [1, 2], "input": [4, 2], "bias": [1]},
            {"output": [4, 1]},
        ),
        "Relu_2": ({"input": [4, 1]}, {"output": [4, 1]}),
    }


def test_extract_shapes_logical_4():
    model = Model()
    conv_1 = Convolution2D(kernel_size=3, out_channels=3)
    conv_2 = Convolution2D(kernel_size=3, out_channels=5)
    conv_3 = Convolution2D(kernel_size=2, out_channels=5)
    relu_1 = Relu()
    relu_2 = Relu()
    relu_3 = Relu()
    conv_1.set_shapes(input=[5, 4, 60, 60])
    model += conv_1.connect(input="input", weight="weight")
    model += relu_1
    model += conv_2
    model += relu_2
    model += conv_3
    model += relu_3
    name_mappings = define_unique_names(model.dag.keys())
    uni_cache: dict[UniadicRecord, str] = {}
    var_cache: dict[Variadic, str] = {}
    conn_info = model.extract_connection_info(name_mappings)
    model_shapes = {
        sub_model_name: sub_model.get_shapes(uni_cache, var_cache, symbolic=False)
        for sub_model, sub_model_name in name_mappings.items()
    }
    shape_info = get_summary_shapes(model_shapes, conn_info)
    assert shape_info == {
        "Convolution2D_0": (
            {
                "weight": [3, 4, 3, 3],
                "input": [5, 4, 60, 60],
                "bias": [1, 3, 1, 1],
                "padding": None,
                "stride": None,
                "dilation": None,
                "groups": None,
            },
            {"output": [5, 3, 58, 58]},
        ),
        "Relu_0": ({"input": [5, 3, 58, 58]}, {"output": [5, 3, 58, 58]}),
        "Convolution2D_1": (
            {
                "weight": [5, 3, 3, 3],
                "padding": None,
                "stride": None,
                "dilation": None,
                "groups": None,
                "input": [5, 3, 58, 58],
                "bias": [1, 5, 1, 1],
            },
            {"output": [5, 5, 56, 56]},
        ),
        "Relu_1": ({"input": [5, 5, 56, 56]}, {"output": [5, 5, 56, 56]}),
        "Convolution2D_2": (
            {
                "weight": [5, 5, 2, 2],
                "padding": None,
                "stride": None,
                "dilation": None,
                "groups": None,
                "input": [5, 5, 56, 56],
                "bias": [1, 5, 1, 1],
            },
            {"output": [5, 5, 55, 55]},
        ),
        "Relu_2": ({"input": [5, 5, 55, 55]}, {"output": [5, 5, 55, 55]}),
    }


def test_extract_shapes_logical_5():
    model = Model()
    linear_1 = Linear(dimension=4)
    linear_2 = Linear(dimension=2)
    linear_3 = Linear(dimension=1)
    relu_1 = Relu()
    relu_2 = Relu()
    relu_3 = Relu()

    model += linear_1.connect(input="input", weight="weight", bias="bias")
    model += relu_1
    model += linear_2
    model += relu_2
    model += linear_3
    model += relu_3
    relu_2.set_shapes(input=[None, None])
    name_mappings = define_unique_names(model.dag.keys())
    uni_cache: dict[UniadicRecord, str] = {}
    var_cache: dict[Variadic, str] = {}
    conn_info = model.extract_connection_info(name_mappings)
    model_shapes = {
        sub_model_name: sub_model.get_shapes(uni_cache, var_cache, symbolic=True)
        for sub_model, sub_model_name in name_mappings.items()
    }
    shape_info = get_summary_shapes(model_shapes, conn_info)
    assert shape_info == {
        "Linear_0": (
            {"weight": [4, "u1"], "input": ["u2", "u1"], "bias": [4]},
            {"output": ["u2", 4]},
        ),
        "Relu_0": ({"input": ["u2", 4]}, {"output": ["u2", 4]}),
        "Linear_1": (
            {"weight": [2, 4], "input": ["u2", 4], "bias": [2]},
            {"output": ["u2", 2]},
        ),
        "Relu_1": ({"input": ["u2", 2]}, {"output": ["u2", 2]}),
        "Linear_2": (
            {"weight": [1, 2], "input": ["u2", 2], "bias": [1]},
            {"output": ["u2", 1]},
        ),
        "Relu_2": ({"input": ["u2", 1]}, {"output": ["u2", 1]}),
    }


def test_define_unique_names_1():
    model = Model()
    lin_0 = Linear()
    lin_1 = Linear()
    lin_2 = Linear()
    lin_3 = Linear()
    lin_4 = Linear()
    lin_5 = Linear()
    lin_6 = Linear()
    buffer = Buffer()
    KernelizedSVM_0 = KernelizedSVM(kernel=RBFKernel())
    KernelizedSVM_1 = KernelizedSVM(kernel=RBFKernel())
    model += lin_0
    model += lin_1
    model += lin_2
    model += lin_3
    model += lin_4
    model += lin_5
    model += lin_6
    model += buffer
    model |= KernelizedSVM_0.connect(input1=model.cout)
    model |= KernelizedSVM_1.connect(input1=model.cout)

    lin_0.set_differentiability(input=True)
    name_dict = define_unique_names(model.dag.keys())
    assert name_dict == {
        lin_0: "Linear_0",
        lin_1: "Linear_1",
        lin_2: "Linear_2",
        lin_3: "Linear_3",
        lin_4: "Linear_4",
        lin_5: "Linear_5",
        lin_6: "Linear_6",
        buffer: "Buffer",
        KernelizedSVM_0: "KernelizedSVM_0",
        KernelizedSVM_1: "KernelizedSVM_1",
    }


def test_define_unique_names_2():
    model = Model()
    model += (lin1 := Linear())
    model += (rel_1 := Relu())
    model += (lin2 := Linear())
    model += (rel_2 := Relu())
    name_dict = define_unique_names(model.dag.keys())
    assert name_dict == {
        lin1: "Linear_0",
        lin2: "Linear_1",
        rel_1: "Relu_0",
        rel_2: "Relu_1",
    }


def test_table_1():
    list_1 = [
        [["cell_11", "cell_12"], ["cell_3"], ["cell_4"]],
        [["cell_5"], ["cell_6"], ["cell_7__1", "cell_7____2", "cell_73", "cell_7__4"]],
    ]
    headers = ["header_1", "header_2", "header_3"]
    table = Table(name="sum1")
    table.add_header(headers)
    for list in list_1:
        table.add_row(list)
    table.compile(row_sep="  ")

    cells = table.cell_str
    n_of_rows = cells.count("\n") - 2
    assert n_of_rows == 7
    n_of_cols = cells.index("\n")
    assert n_of_cols == 31


def test_table_2():
    list_1 = [[["a", "b", "c", "d"], ["a", "b", "bc", "cde"], ["ab", "cdef"]]]
    headers = ["", "", ""]
    table = Table(name="sum1")
    table.add_header(headers)
    for list in list_1:
        table.add_row(list)
    table.compile(row_sep="  ")
    cells = table.cell_str
    n_of_rows = cells.count("\n") - 2
    assert n_of_rows == 4
    n_of_cols = cells.index("\n")
    assert n_of_cols == 12


def test_table_3():
    list_1 = [[["a", "b", "c", "d"], ["a", "b", "bc", "cde"], ["ab", "cdef"]]]
    headers = ["header1", "header2", "header3"]
    table = Table(name="sum2")
    table.add_header(headers)
    for list in list_1:
        table.add_row(list)
    table.compile(row_sep="  ")
    cells = table.cell_str
    n_of_rows = cells.count("\n") - 2
    assert n_of_rows == 4
    n_of_cols = cells.index("\n")
    assert n_of_cols == 25


def test_table_4():
    list_1 = [[["a", "b", "c", "d"], ["a", "b", "bc", "cde"], ["ab", "cdef"]]]
    headers = ["header1", "header2", "header3"]
    subheaders = ["subheader__1", "subheader_2", "subheader_3"]
    table = Table(name="sum2")
    table.add_header(headers)
    table.add_header(subheaders)
    for list in list_1:
        table.add_row(list)
    table.compile(row_sep=" | ")
    cells = table.cell_str
    n_of_rows = cells.count("\n") - 2
    assert n_of_rows == 4
    n_of_cols = cells.index("\n")
    assert n_of_cols == 40


def test_table_5():
    list_1 = [[["a", "b", "c", "d"]], [["a", "b", "bc", "cde"]], [["ab", "cdef"]]]
    headers = [""]
    subheaders = [""]
    table = Table(name="sum2")
    table.add_header(headers)
    table.add_header(subheaders)
    for list in list_1:
        table.add_row(list)
    table.compile(row_sep=" | ")
    cells = table.cell_str
    n_of_rows = cells.count("\n") - 2
    assert n_of_rows == 12
    n_of_cols = cells.index("\n")
    assert n_of_cols == 4


def test_physical_summary_1():
    model = Model()
    model += Linear(dimension=5).connect(input="input")
    model += LeakyRelu()
    model += (lin1 := Linear(dimension=3))
    model += (l_relu := LeakyRelu(slope=TBD)).connect(slope=IOKey("slope"))
    l_relu.set_values(slope=Tensor(1e-1))
    model += Relu()
    lin1.set_shapes(input=[3, 5])
    comp_model = mithril.compile(
        model=model, backend=NumpyBackend(), data_keys={"input"}
    )

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_1") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_summary_2():
    model1 = Model()
    model1 += Relu()
    model1 += Sigmoid()
    model = Model()
    model += Linear(dimension=5).connect(input="input")
    model += LeakyRelu()
    model += Linear(dimension=3)
    model += model1
    assert isinstance(model.cin, Connection)
    model.set_differentiability({model.cin: True})

    comp_model = mithril.compile(
        model=model, backend=NumpyBackend(), shapes={"input": [5, 5]}
    )

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_2") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_physical_summary_3():
    model = Model()
    model_1 = KernelizedSVM(kernel=RBFKernel())
    model_1.set_differentiability(input1=True, input2=True)

    model_2 = MLP(
        activations=[Sigmoid(), Tanh(), Relu(), LeakyRelu()], dimensions=[3, 4, 5, 6]
    )
    model += model_1
    model += model_2
    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), jit=False, safe_names=False
    )

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True, types=False)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_3") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_physical_summary_3_logical_with_depth():
    model = Model()
    model_1 = KernelizedSVM(kernel=RBFKernel())
    model_1.set_differentiability(input1=True, input2=True)
    model_1.set_cin("input1")

    model_2 = MLP(
        activations=[Sigmoid(), Tanh(), Relu(), LeakyRelu()], dimensions=[3, 4, 5, 6]
    )
    model += model_1
    model += model_2

    with redirect_stdout(StringIO()) as summary:
        model.summary(
            shapes=True, symbolic=True, types=True, alternative_shapes=True, depth=1
        )

    ref_table = ""
    with open(
        "tests/scripts/summary_txts/test_physical_summary_3_logical_with_depth"
    ) as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_physical_summary_4():
    model = Model()
    model_1 = KernelizedSVM(kernel=RBFKernel())
    model_1.set_differentiability(input1=True, input2=True)
    model_1.set_cin("input1")
    model_2 = MLP(
        activations=[Sigmoid(), Tanh(), Relu(), LeakyRelu()], dimensions=[3, 4, 5, 6]
    )
    model += model_1
    model += model_2
    comp_model = mithril.compile(model=model, backend=TorchBackend(), safe_names=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=model_2, shapes=True, verbose=True, depth=1)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_4") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


# TODO: Fix the bug below
@pytest.mark.skip("When replace with primitive applied the summary is broken")
def test_physical_summary_5():
    model = Model()
    model_1 = MLP(
        activations=[Sigmoid(), Relu(), Relu(), Sigmoid()], dimensions=[6, 7, 8, 9]
    )
    model_2 = MLP(activations=[Sigmoid(), Relu(), Relu()], dimensions=[3, 7, 9])
    model += model_1
    model += model_2
    comp_model = mithril.compile(model=model, backend=JaxBackend(), safe_names=False)
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, model=model_2, depth=1)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_5") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_model_summary_5():
    model = Model()
    add = Add()
    divide = Divide()
    exp = Power()
    add_shape: ShapeTemplateType = ["u1", "u2"]
    add.set_shapes(left=add_shape, right=[1])
    div_shape: ShapeTemplateType = ["u3", "u4"]
    divide.set_shapes(numerator=div_shape, denominator=[1])
    exp.set_shapes(base=div_shape, exponent=[1])
    model += add
    model += divide
    model += exp
    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), safe_names=False, inference=True
    )

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_model_summary_5") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_model_summary_6():
    model = Model()
    random_kernel_model = Model()
    random_kernel_model |= (add1 := Add()).connect(left="input1", right="input2")
    random_kernel_model |= (relu1 := Relu()).connect(input=add1.output)
    random_kernel_model |= Sigmoid().connect(input=relu1.output, output="output")
    model |= random_kernel_model.connect(input1="input1", input2="input2")
    model |= Linear().connect(
        input=random_kernel_model.output,  # type: ignore
        weight="weight",
        bias="b",
        output="output",
    )
    random_kernel_model.set_shapes(input1=["N", "M"], input2=["N", "M"])

    comp_model = mithril.compile(model=model, backend=JaxBackend(), safe_names=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_model_summary_6") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_model_summary_7():
    random_kernel_model = Model()
    random_kernel_model |= (add1 := Add()).connect(left="input1", right="input2")
    random_kernel_model |= (relu1 := Relu()).connect(input=add1.output)
    random_kernel_model |= (sig1 := Sigmoid()).connect(input=relu1.output)
    random_kernel_model |= Linear().connect(
        input=sig1.output, weight="weight", bias="b", output="output"
    )
    random_kernel_model.set_shapes(input1=["N", "M"], input2=["N", "M"])

    comp_model = mithril.compile(
        model=random_kernel_model, backend=JaxBackend(), safe_names=False
    )

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_model_summary_7") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_model_summary_8():
    model = Model()
    random_kernel_model = Model()
    another_random_model = Model()
    another_random_model |= Add().connect(
        left=IOKey("input1", type=Tensor),
        right=IOKey("input2", type=Tensor),
        output="output",
    )
    input1_shape: ShapeTemplateType = ["a", ("Var1", ...), "b"]
    another_random_model.set_shapes(input1=input1_shape)
    another_random_model.set_cin("input1")

    random_kernel_model |= (add1 := Add()).connect(left="input1", right="input2")
    random_kernel_model |= (relu1 := Relu()).connect(input=add1.output)
    random_kernel_model |= Sigmoid().connect(input=relu1.output)
    random_kernel_model.set_shapes(input1=["N", "M"], input2=["N", "M"])
    another_random_model.set_cin("input1")

    model += random_kernel_model
    model += another_random_model

    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), safe_names=False, inference=True
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=False)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_model_summary_8") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_model_summary_9():
    model = Model()
    random_kernel_model = Model()
    random_kernel_model |= (add1 := Relu()).connect(input="input1")
    random_kernel_model |= (relu1 := Relu()).connect(input=add1.output)
    random_kernel_model |= Sigmoid().connect(input=relu1.output)
    random_kernel_model.set_shapes(input1=["N", "M"])
    model += random_kernel_model
    model += Relu()

    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), safe_names=False, inference=True
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_model_summary_9") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_physical_summary_10():
    model = Model()
    sig_model1 = Sigmoid()
    sig_model2 = Sigmoid()
    model |= sig_model1.connect(input="input", output=IOKey("output1"))
    model |= sig_model2.connect(input="input", output=IOKey("output2"))
    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), jit=False, safe_names=False, inference=True
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(
            verbose=True, shapes=True, symbolic=True, model=sig_model1, types=True
        )

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_10") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_physical_summary_11():
    model = Model()
    sig_model1 = Sigmoid()
    sig_model2 = Sigmoid()
    model |= sig_model1.connect(input="input", output=IOKey(name="output1"))
    model |= sig_model2.connect(input="input", output=IOKey(name="output2"))
    comp_model = mithril.compile(
        model=model,
        backend=JaxBackend(),
        safe_names=False,
        inference=True,
        jit=False,
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True, model=sig_model2)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_11") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_summary_12():
    model = Model()
    sig_model1 = Sigmoid()
    sig_model2 = Sigmoid()
    model |= sig_model1.connect(input="input", output=IOKey(name="output1"))
    model |= sig_model2.connect(input="input", output=IOKey(name="output2"))
    comp_model = mithril.compile(
        model=model, backend=JaxBackend(), safe_names=False, inference=True
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_12") as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table


def test_physical_summary_13():
    model = Model()
    sig_model1 = Sigmoid()
    sig_model2 = Sigmoid()
    sig_model3 = Sigmoid()
    model |= sig_model1.connect(input="input", output="output1")
    model |= sig_model2.connect(input="input", output="output2")
    model.set_cout("output2")
    comp_model = mithril.compile(model=model, backend=JaxBackend(), inference=True)
    with pytest.raises(ValueError) as err_info:
        comp_model.summary(model=sig_model3)
    assert str(err_info.value) == "Given model is not a part of compiled model"


def test_physical_summary_14():
    model = Model()
    sig_model1 = Add()
    sig_model1.set_types(left=Tensor, right=Tensor)
    sig_model2 = Add()
    model |= sig_model1.connect(left="left", right="right", output=IOKey("output1"))
    model |= sig_model2.connect(left="left", right="right", output=IOKey("output2"))
    comp_model = mithril.compile(
        model=model,
        backend=JaxBackend(),
        shapes={"left": [3, 4, 5]},
        safe_names=False,
        inference=True,
    )
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=sig_model2, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_14") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


# TODO: Fix the bug below
@pytest.mark.skip("When replace with primitive applied the summary is broken")
def test_physical_summary_15():
    model = Model()
    lin_model_1 = Linear(dimension=3)
    lin_model_2 = Linear(dimension=3)
    lin_model_3 = Linear(dimension=3)
    lin_model_4 = Linear(dimension=3)
    model |= lin_model_1.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output1")
    )
    model |= lin_model_2.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output2")
    )
    model |= lin_model_3.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output3")
    )
    model |= lin_model_4.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output4")
    )
    lin_model_1.set_differentiability(input=True)

    comp_model = mithril.compile(model=model, backend=JaxBackend(), jit=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=lin_model_4, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_15") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


# TODO: Fix the bug below
@pytest.mark.skip("When replace with primitive applied the summary is broken")
def test_physical_summary_16():
    model = Model()
    lin_model_1 = Linear(dimension=3)
    *_, add_model_1 = tuple(lin_model_1.dag.keys())
    lin_model_2 = Linear(dimension=3)
    lin_model_3 = Linear(dimension=3)

    model |= lin_model_1.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output1")
    )
    model |= lin_model_2.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output2")
    )
    model |= lin_model_3.connect(
        input="input", weight="weight", bias="b", output=IOKey(name="output3")
    )

    comp_model = mithril.compile(model=model, backend=JaxBackend(), safe_names=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=add_model_1, verbose=True, types=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_16") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


# TODO: Fix the bug below
@pytest.mark.skip("When replace with primitive applied the summary is broken")
def test_physical_summary_17():
    model = Model()
    lin_model_1 = Linear(dimension=3)
    _, matmul_model_1, _ = tuple(lin_model_1.dag.keys())
    lin_model_2 = Linear(dimension=3)
    lin_model_3 = Linear(dimension=3)
    model |= lin_model_1.connect(
        input="input", weight="weight", bias="b", output="output1"
    )
    model |= lin_model_2.connect(
        input="input", weight="weight", bias="b", output="output2"
    )
    model |= lin_model_3.connect(
        input="input", weight="weight", bias="b", output="output3"
    )
    model.set_cout("output3")
    lin_model_1.set_differentiability(input=True)

    comp_model = mithril.compile(model=model, backend=JaxBackend(), safe_names=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=matmul_model_1, verbose=True, types=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_physical_summary_17") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_resnet_18_physical_summary():
    model = resnet18(1)
    assert isinstance(model.cin, Connection)
    model.set_differentiability({model.cin: True})

    comp_model = mithril.compile(model=model, backend=TorchBackend(), jit=False)

    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(verbose=True, shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_resnet_18_physical_summary") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_resnet18_summary():
    model = resnet18(1)
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_resnet18_summary") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_logical_model_summary_1():
    model = MLP(
        activations=[Sigmoid(), Relu(), Relu(), Tanh()], dimensions=[32, 12, 14, 71]
    )
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_1") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_logical_model_summary_2():
    model = Model()
    model |= Convolution2D(kernel_size=4, out_channels=4).connect()
    model += Relu()
    model += Convolution2D(kernel_size=4, out_channels=4)
    model += LeakyRelu()
    model += Flatten(start_dim=1)
    model += Linear(dimension=1)
    model += Sum()

    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_2") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_logical_model_summary_3():
    model = Model()
    model |= Add().connect(
        left=IOKey("input1", type=Tensor),
        right=IOKey("input2", type=Tensor),
        output="output1",
    )
    model |= Add().connect(
        left="input1",
        right=IOKey("input3", type=Tensor),
        output="output2",
    )
    model |= Add().connect(left="input2", right="input3", output="output3")
    model.expose_keys("output1", "output2", "output3")
    model.set_cin("input1")
    model.set_cout("output1")

    model_1 = Model()
    model_1 |= (m1 := deepcopy(model)).connect()
    model_1 |= (m2 := deepcopy(model)).connect(
        input1=m1.output1,  # type: ignore
        input2=m1.output2,  # type: ignore
        input3=m1.output3,  # type: ignore
    )
    model_1 |= deepcopy(model).connect(
        input1=m2.output1,  # type: ignore
        input2=m2.output2,  # type: ignore
        input3=m2.output3,  # type: ignore
        output1="output2",
        output2="output3",
        output3="output4",
    )
    model_1.set_cout("output2")
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_3") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_4():
    model_n = Model()
    add = Add()
    add.set_cin("left")
    model_n |= add
    add.set_types(left=Tensor, right=Tensor)
    for _ in range(5):
        model_n += deepcopy(model_n)

    with redirect_stdout(StringIO()) as summary:
        model_n.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_4") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_5():
    model = Model()
    model += create_layer(16)
    model += create_layer(32)
    model += Flatten(start_dim=1)
    model += Linear(1000)
    model += Linear(1)

    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_5") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_logical_model_summary_6():
    model = Model()
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_6") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_7():
    model_1 = Model()
    buff_1 = Sigmoid()
    buff_2 = Sigmoid()
    model_1 |= buff_1.connect(input="input", output="output1")
    model_1 |= buff_2.connect(input="output1", output="output2")
    model_1.expose_keys("output1", "output2")
    model_n = Model()
    for model in (deepcopy(model_1) for n in range(3)):
        model_n += model
    model_nm = Model()
    for model in (deepcopy(model_n) for m in range(3)):
        model_nm += model

    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_7") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_8():
    model = Model()
    sig1, sig2, sig3, sig4 = Sigmoid(), Sigmoid(), Sigmoid(), Sigmoid()
    model |= sig1.connect(input="input")
    model |= sig2.connect(input=sig1.output, output="out_1")
    model |= sig3.connect(input=sig1.output, output="out_2")
    out_name = "outputoutputoutputoutputoutputoutputoutputoutputoutputoutput3"
    model |= sig4.connect(
        input=sig1.output,
        output=out_name,
    )
    model.expose_keys("out_1", "out_2", out_name)
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_8") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_9():
    model = Model()
    add_1, add_2 = Add(), Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    add_1.set_cin("left")
    add_2.set_cin("left")
    model |= add_1.connect(left="left")
    model.merge_connections(add_1.left, add_1.right)
    model |= add_2.connect(output=add_1.left, left="left_1")
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_9") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_10():
    model = Model()
    add_1, add_2 = Add(), Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    add_1.set_cin("left")
    add_2.set_cin("left")
    model |= add_1.connect(left="left", right="right", output="output")
    model |= add_2.connect(left=add_1.left, output="output1")
    model.expose_keys("output", "output1")

    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=False)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_10") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_11():
    model = Model()
    sig_1, sig_2, sig_3 = Sigmoid(), Sigmoid(), Sigmoid()
    model |= sig_1.connect(input="input1", output="output1")
    model |= sig_2.connect(input="input2", output="output2")
    model |= sig_3.connect(input="input3", output="output3")
    model.expose_keys("output1", "output2", "output3")
    model.set_cin("input1")
    model.set_cout("output1")

    model_1, model_2, model_3 = deepcopy(model), deepcopy(model), deepcopy(model)

    model_n = Model()

    model_n |= model_3.connect(
        output1="output1",
        output2="output2",
        output3="output3",
    )
    model_n.merge_connections(model_3.input1, model_3.input2, model_3.input3)  # type: ignore
    model_n |= model_2.connect(
        output1=model_3.input1,  # type: ignore
        output2="output4",
        output3="output5",
    )
    model_n.merge_connections(model_2.input1, model_2.input2, model_2.input3)  # type: ignore
    model_n |= model_1.connect(output1=model_2.input1)  # type: ignore
    model_n.expose_keys("output1", "output2", "output3", "output4", "output5")

    with redirect_stdout(StringIO()) as summary:
        model_n.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_11") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_12():
    model = Model()
    sig_1, sig_2, sig_3 = Sigmoid(), Sigmoid(), Sigmoid()
    model |= sig_1.connect(input="input1", output="output1")
    model |= sig_2.connect(input="input2", output="output2")
    model |= sig_3.connect(input="input3", output="output3")
    model.expose_keys("output1", "output2", "output3")

    model_1, model_2, model_3 = deepcopy(model), deepcopy(model), deepcopy(model)

    model_n = Model()

    model_n |= model_3.connect(
        output1="output1",
        output2="output2",
        output3="output3",
    )
    model_n |= model_1.connect(input1="input1", input2="input2", input3="input3")  # type: ignore
    model_n.merge_connections(model_3.input1, model_3.input2, model_3.input3)  # type: ignore
    model_n |= model_2.connect(
        input1=model_1.output1,  # type: ignore
        input2=model_1.output2,  # type: ignore
        input3=model_1.output3,  # type: ignore
        output1=model_3.input1,  # type: ignore
        output2="output4",
        output3="output5",
    )
    model_n.expose_keys("output1", "output2", "output3", "output4", "output5")

    with redirect_stdout(StringIO()) as summary:
        model_n.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_12") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_logical_model_summary_13():
    model = Model()
    model1 = Model()
    linear1 = Linear()
    linear2 = Linear()
    linear3 = Linear()
    model |= linear1.connect(output="output1")
    model |= linear2.connect(input=model.output1)  # type: ignore
    model.expose_keys("output1")
    model1 += model
    model1 |= linear3.connect(input=model.output1)  # type: ignore
    model1.set_cout(linear3.output)

    with redirect_stdout(StringIO()) as summary:
        model1.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_logical_model_summary_13") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_simple_extend_from_input_summary():
    model = Model()
    model |= Linear(dimension=5).connect(input="lin", output="output")
    model |= Linear(dimension=3).connect(input="input", output="lin")

    model._freeze()

    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_simple_extend_from_input_summary") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_primitive_model_summary_1():
    model = Relu()
    with redirect_stdout(StringIO()) as summary:
        model.summary()

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_1") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_primitive_model_summary_2():
    model = Mean()
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_2") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_primitive_model_summary_3():
    model = ToTensor()
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_3") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def generate_comp_model():
    matmul = MatrixMultiply()
    add = Add()
    sig = Sigmoid()
    l_relu = LeakyRelu()
    test_model = Model()
    test_model |= matmul.connect(left="left", right="right")
    test_model |= add.connect(left=IOKey("in1", type=Tensor), right=matmul.output)
    test_model |= sig.connect(input=add.output)
    test_model |= l_relu.connect(input=sig.output, output="output")
    comp_model = mithril.compile(model=test_model, backend=JaxBackend(), inference=True)
    return comp_model, matmul, add, sig, l_relu, test_model


def test_primitive_model_summary_4():
    with redirect_stdout(StringIO()) as summary:
        comp_model, matmul, _, _, _, _ = generate_comp_model()
        comp_model.summary(shapes=True, symbolic=True, model=matmul, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_4") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_primitive_model_summary_5():
    with redirect_stdout(StringIO()) as summary:
        comp_model, _, add, _, _, _ = generate_comp_model()
        comp_model.summary(shapes=True, symbolic=True, model=add, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_5") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_primitive_model_summary_6():
    with redirect_stdout(StringIO()) as summary:
        comp_model, _, _, sig, _, _ = generate_comp_model()
        comp_model.summary(shapes=True, symbolic=True, model=sig, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_6") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_primitive_model_summary_7():
    with redirect_stdout(StringIO()) as summary:
        comp_model, _, _, _, l_relu, _ = generate_comp_model()
        comp_model.summary(shapes=True, symbolic=True, model=l_relu, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_7") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_primitive_model_summary_8():
    with redirect_stdout(StringIO()) as summary:
        comp_model, _, _, _, _, test_model = generate_comp_model()
        comp_model.summary(shapes=True, symbolic=True, model=test_model, verbose=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_8") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_primitive_model_summary_9():
    model = Concat(axis=4)
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_primitive_model_summary_9") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_summary_nontensor_models():
    model = Model()
    mean_model = Mean()
    shape_model = Shape()
    size_model = Size()
    lin_model = Linear()
    to_tensor_model = ToTensor()

    model |= lin_model.connect(input="input", weight="weight", bias="b")
    model |= shape_model.connect(input=lin_model.output, output="output1")
    model |= mean_model.connect(input=lin_model.output, output="output2")
    model |= size_model.connect(input=lin_model.output, output="output3")
    model |= to_tensor_model.connect(input=size_model.output, output="output4")
    model.expose_keys("output1", "output2", "output3", "output4")
    with redirect_stdout(StringIO()) as summary:
        model.summary(shapes=True, symbolic=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_summary_nontensor_models") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary():
    model = MLP(
        activations=[Relu(), Relu(), LeakyRelu(), Sigmoid()], dimensions=[7, 11, 4, 3]
    )
    ctx = TrainModel(model)
    ctx.add_loss(
        SquaredError(),
        input=model.output,
        target="target",
        reduce_steps=[Mean()],
        coef=Tensor(0.1),
    )
    ctx.add_regularization(L1(), coef=0.1, input="weight1")
    with redirect_stdout(StringIO()) as summary:
        ctx.summary()

    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary_2():
    model = Model()
    add_1 = Add()
    add_2 = Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    matmul_1 = MatrixMultiply()
    model |= add_1.connect(left="input1", right="input2", output="output1")
    model |= add_2.connect(left="input3", right="input4", output="output2")
    model |= matmul_1.connect(left="input5", right="input6", output="output3")
    model.expose_keys("output1", "output2", "output3")
    ctx = TrainModel(model)
    ctx.add_loss(
        SquaredError(),
        input=model.output1,  # type: ignore
        target="target1",
        reduce_steps=[Mean()],
    )
    ctx.add_loss(
        CrossEntropy(),
        input=model.output2,  # type: ignore
        target="target2",
        reduce_steps=[Sum()],
    )
    loss_add = Add()
    loss_add.set_types(left=Tensor, right=Tensor)
    ctx.add_loss(loss_add, left=model.output3, right="right", reduce_steps=[Min()])  # type: ignore
    with redirect_stdout(StringIO()) as summary:
        ctx.summary(symbolic=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_2") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_traincontext_summary_3():
    model = Model()
    add_1 = Add()
    add_2 = Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    add_1.set_cin("left")
    add_2.set_cin("left")
    matmul_1 = MatrixMultiply()
    model |= add_1.connect(
        left=IOKey("in1", differentiable=True),
        right="in2",
        output="output1",
    )
    model |= add_2.connect(left="", output="output2")
    model |= matmul_1.connect(left="", output="output3")
    model.expose_keys("output1", "output2", "output3")
    model.set_cin(matmul_1.left)
    ctx = TrainModel(model)
    ctx.add_loss(
        SquaredError(),
        input=model.output1,  # type: ignore
        target="target",
        reduce_steps=[Mean()],
    )
    ctx.add_loss(
        CrossEntropy(),
        input=model.output2,  # type: ignore
        target="target2",
        reduce_steps=[Sum()],
    )
    loss_add = Add()
    loss_add.set_types(left=Tensor, right=Tensor)
    ctx.add_loss(loss_add, left=model.output3, right="target3", reduce_steps=[Min()])  # type: ignore
    ctx.add_regularization(L1(), input=add_1.left, coef=0.1)

    with redirect_stdout(StringIO()) as summary:
        ctx.summary(symbolic=True, types=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_3") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary_4():
    model = Model()
    add_1 = Add()
    add_2 = Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    add_1.set_cin("left")
    add_2.set_cin("left")
    matmul_1 = MatrixMultiply()
    model |= add_1.connect(
        left=IOKey("in1", differentiable=True),
        right=IOKey("in2", differentiable=True),
        output="output1",
    )
    model |= add_2.connect(left="", output="output2")
    model |= matmul_1.connect(left="", output="output3")
    model.expose_keys("output1", "output2", "output3")
    model.set_cin(matmul_1.left)

    ctx = TrainModel(model)
    ctx.add_loss(
        SquaredError(),
        input=model.output1,  # type: ignore
        target="target1",
        reduce_steps=[Mean(axis=-1)],
    )
    ctx.add_loss(
        SquaredError(),
        input=model.output2,  # type: ignore
        target="target2",
        reduce_steps=[Sum(axis=1), Max(axis=2), Mean(axis=-1)],
    )
    loss_add = Add()
    loss_add.set_types(left=Tensor, right=Tensor)
    ctx.add_loss(loss_add, left=model.output3, right="right")  # type: ignore
    ctx.add_regularization(L1(), input=add_1.left, coef=0.1)
    ctx.add_regularization(L1(), input=add_1.right, coef=0.1)

    with redirect_stdout(StringIO()) as summary:
        ctx.summary(shapes=False, types=True)

    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_4") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary_5():
    model = Model()
    add_1 = Add()
    add_2 = Add()
    add_1.set_types(left=Tensor, right=Tensor)
    add_2.set_types(left=Tensor, right=Tensor)
    add_1.set_cin("left")
    add_2.set_cin("left")
    matmul_1 = MatrixMultiply()
    model |= add_1.connect(
        left=IOKey("in1", differentiable=True),
        right=IOKey("in2", differentiable=True),
        output="output1",
    )
    model += add_2.connect(output="output2")
    model += matmul_1.connect(output="output3")
    model.expose_keys("output1", "output2", "output3")
    ctx = TrainModel(model)
    ctx.add_loss(
        SquaredError(),
        input=model.output1,  # type: ignore
        target="target",
        reduce_steps=[Mean(axis=-1)],
    )
    ctx.add_loss(
        SquaredError(),
        input=model.output2,  # type: ignore
        target="target",
        reduce_steps=[Sum(axis=1), Max(axis=2), Mean(axis=-1)],
    )
    ctx.add_loss(Add(), left=model.output3, right="right")  # type: ignore
    ctx.add_regularization(L1(), input=add_1.left, coef=Tensor(0.1))
    ctx.add_regularization(L1(), input=add_1.right, coef=Tensor(0.1))
    comp_model = mithril.compile(model=ctx, backend=NumpyBackend(), safe_shapes=False)
    with redirect_stdout(StringIO()) as summary:
        comp_model.summary(model=add_1, verbose=True)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_5") as f:
        ref_table = f.read()

    assert "\n" + summary.getvalue() == ref_table


def test_traincontext_summary_resnet():
    model = resnet34(1)
    ctx = TrainModel(model)
    ctx.add_loss(SquaredError(), input="output", target="target", reduce_steps=[Mean()])
    ctx.add_regularization(L1(), input="weight_20", coef=0.1)
    with redirect_stdout(StringIO()) as summary:
        ctx.summary(depth=1)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_resnet") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary_regex_reg():
    model = MLP(
        dimensions=[10 for _ in range(10)], activations=[Relu() for _ in range(10)]
    )
    ctx = TrainModel(model)
    ctx.add_loss(SquaredError(), input="output", target="target", reduce_steps=[Mean()])
    ctx.add_regularization(L2(), input=re.compile("weight\\d"), coef=0.1)
    ctx.add_regularization(L1(), input=re.compile("weight\\d"), coef=0.1)
    with redirect_stdout(StringIO()) as summary:
        ctx.summary(depth=1)
    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_regex_reg") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_traincontext_summary_7():
    model = Model()
    model += MLP(
        dimensions=[10 for _ in range(10)], activations=[Relu() for _ in range(10)]
    ).connect(input="input", output=IOKey(name="output"))
    model.expose_keys("output")

    ctx = TrainModel(model)

    reg_model = Model()
    reg_model += Relu().connect(input="foo", output=IOKey(name="output"))

    loss_model = Model()
    loss_model += Add().connect(
        left=IOKey("l1", type=Tensor),
        right=IOKey("r1", type=Tensor),
        output=IOKey(name="out"),
    )
    ctx.add_loss(loss_model, l1="output", r1="target", reduce_steps=[Mean()])
    ctx.add_regularization(reg_model, foo=re.compile("weight\\d"), coef=0.1)
    ctx.add_regularization(L1(), input=re.compile("weight\\d"), coef=0.1)
    with redirect_stdout(StringIO()) as summary:
        ctx.summary()
    ref_table = ""
    with open("tests/scripts/summary_txts/test_traincontext_summary_7") as f:
        ref_table = f.read()

    assert summary.getvalue() == ref_table


def test_summary_of_nested_composite_model_with_names():
    lin = Linear(name="lin")
    model = Model(name="my_model")
    model += lin

    with redirect_stdout(StringIO()) as summary:
        model.summary()

    ref_table = ""
    with open(
        "tests/scripts/summary_txts/test_summary_of_nested_composite_model_with_names"
    ) as f:
        ref_table = f.read()
    assert summary.getvalue() == ref_table

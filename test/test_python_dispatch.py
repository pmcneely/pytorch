# Owner(s): ["module: __torch_dispatch__"]

import tempfile
import torch
from copy import deepcopy
from torch.library import Library
from torch.cuda.jiterator import _create_jit_fn
from torch.testing._internal.common_utils import TestCase, run_tests, TEST_WITH_ROCM
from torch.testing._internal.logging_tensor import LoggingTensor, LoggingTensorReentrant, LoggingTensorMode, \
    log_input, capture_logs, no_dispatch
from torch.utils._pytree import tree_map
from torch.utils._python_dispatch import enable_torch_dispatch_mode, push_torch_dispatch_mode, TorchDispatchMode

import logging
from functools import partial

class TestPythonRegistration(TestCase):
    def test_override_aten_ops_with_multiple_libraries(self) -> None:
        x = torch.tensor([1, 2])
        my_lib1 = Library("aten", "IMPL")
        my_lib2 = Library("aten", "IMPL")

        # Example 1
        def my_neg(*args, **kwargs):
            return args[0]._neg_view()

        # Now we are secretly making the operator a view op so autograd needs to know how
        # to handle it
        my_lib1.impl('neg', my_neg, "AutogradCPU")

        self.assertTrue(torch.neg(x).is_neg())

        # RuntimeError: impl("aten::neg", ...):
        # Explicitly provided namespace (aten) in operator name does not match ...
        with self.assertRaisesRegex(RuntimeError, "operator name does not match namespace"):
            my_lib3 = Library("foo", "IMPL")
            my_lib3.impl(torch.ops.aten.neg.default, my_neg, "AutogradCPU")
            del my_lib3

        # Example 2
        def my_mul(*args, **kwargs):
            return torch.zeros_like(args[0])

        # torch.ops.aten.mul.Tensor
        my_lib2.impl("aten::mul.Tensor", my_mul, "ZeroTensor")

        y = torch._efficientzerotensor(2)
        self.assertFalse(torch.mul(x, y)._is_zerotensor())

        # Assert that a user can't override the behavior of a (ns, op, dispatch_key)
        # combination if someone overrided the behavior for the same before them
        with self.assertRaisesRegex(RuntimeError, 'already a kernel registered from python'):
            my_lib2.impl(torch.ops.aten.mul.Tensor, my_mul, "ZeroTensor")

        del my_lib1

        # Validate that lib2 is not affected by removing lib1
        self.assertFalse(torch.mul(x, y)._is_zerotensor())

        del my_lib2

        # Validate that the old behavior is restored for neg and mul
        self.assertFalse(torch.neg(x).is_neg())
        self.assertTrue(torch.mul(x, y)._is_zerotensor())

    def test_override_cpu_sum(self) -> None:
        # Example 1
        run = [False]

        def my_sum(*args, **kwargs):
            run[0] = True
            return args[0]

        my_lib1 = Library("aten", "IMPL")
        my_lib1.impl('aten::sum', my_sum, "CPU")
        x = torch.tensor([1, 2])
        self.assertEqual(torch.sum(x), x)
        self.assertTrue(run[0])
        del my_lib1
        # Validate that the old behavior is restored for sum
        self.assertEqual(torch.sum(x), torch.tensor(3))

    def test_override_cuda_with_jiterator(self) -> None:
        def override_where_cuda() -> None:
            # Example 1: Invert the behavior of where's condition input
            not_where_code_string = '''
            template <typename T> T inverted_where(bool cond, T a, T b){
                return !cond ? a : b;
            }
            '''
            jitted_where = _create_jit_fn(not_where_code_string)

            CALLED = [False]

            def inverted_where(*args, **kwargs):
                CALLED[0] = True
                return jitted_where(*args, **kwargs)

            # overriding where's cuda kernel with Jiterator generated kernel
            my_lib = Library("aten", "IMPL")
            my_lib.impl('aten::where.self', inverted_where, "CUDA")

            device = 'cuda'
            cond = torch.tensor([True, True, False], device=device, dtype=torch.bool)
            x = torch.tensor([1, 2, 3], device=device)
            y = torch.tensor([-1, -2, -3], device=device)

            self.assertEqual(torch.where(cond, x, y), torch.tensor([-1, -2, 3]))
            self.assertTrue(CALLED[0])
            del my_lib

            # behavior restored after deregistration
            self.assertEqual(torch.where(cond, x, y), torch.tensor([1, 2, -3]))

        def override_gelu_cuda() -> None:
            # Example 2: Use relu to approximate gelu for faster compute
            fastest_gelu_code_string = '''
            template <typename T> T fast_gelu(T a){
                return a > 0 ? a : 0;
            }
            '''
            jitted_gelu = _create_jit_fn(fastest_gelu_code_string)

            CALLED = [False]

            def fast_gelu(*args, **kwargs):
                CALLED[0] = True
                return jitted_gelu(*args, **kwargs)

            # overriding gelu's cuda kernel with Jiterator generated relu kernel
            my_lib = Library("aten", "IMPL")
            my_lib.impl('aten::gelu', fast_gelu, "CUDA")

            x = torch.rand([3, 3], device='cuda', dtype=torch.float)
            self.assertEqual(torch.nn.functional.gelu(x), torch.nn.functional.relu(x))
            self.assertTrue(CALLED[0])
            del my_lib

            # behavior restored after deregistration
            self.assertNotEqual(torch.nn.functional.gelu(x), torch.nn.functional.relu(x))

        def override_exp_cuda() -> None:
            # Example 3: Preventing exp from exploding for float16
            clipped_exp_code_string = '''
            template <typename T> T clipped_exp(T a){
                return a > T(10.0) ? T(22026.4657948) : exp(a);
            }
            '''
            jitted_exp = _create_jit_fn(clipped_exp_code_string)

            CALLED = [False]

            def clipped_exp(*args, **kwargs):
                CALLED[0] = True
                return jitted_exp(*args, **kwargs)

            # overriding exp's cuda kernel with clipped_exp kernel
            my_lib = Library("aten", "IMPL")
            my_lib.impl('aten::exp', clipped_exp, "CUDA")

            x = torch.tensor([0.0, 100.0], device='cuda', dtype=torch.float16)
            self.assertEqual(torch.exp(x), torch.tensor([1.0, 22026.4657948], dtype=torch.float16))
            self.assertTrue(CALLED[0])
            del my_lib

            # behavior restored after deregistration
            self.assertEqual(torch.exp(x), torch.tensor([1.0, torch.inf], dtype=torch.float16))

        def override_add_cuda() -> None:
            # Example 4: simulate a hardware bug, where the adder is always off by 1
            buggy_add_code_string = '''
            template <typename T> T buggy_add(T a, T b){
                return a + b + T(1);
            }
            '''
            jitted_add = _create_jit_fn(buggy_add_code_string)

            CALLED = [False]

            def buggy_add(*args, **kwargs):
                CALLED[0] = True
                return jitted_add(*args, **kwargs)

            my_lib = Library("aten", "IMPL")
            my_lib.impl('aten::add.Tensor', buggy_add, "CUDA")

            x_cpu = torch.rand([3, 3], device='cpu')
            y_cpu = torch.rand([3], device='cpu')

            x_cuda = x_cpu.cuda()
            y_cuda = y_cpu.cuda()

            self.assertEqual(x_cuda + y_cuda, x_cpu + y_cpu + 1)
            self.assertTrue(CALLED[0])
            del my_lib

            # behavior restored after deregistration
            self.assertEqual(x_cuda + y_cuda, x_cpu + y_cpu)

        if torch.cuda.is_available() and not TEST_WITH_ROCM:
            override_where_cuda()
            override_gelu_cuda()
            override_exp_cuda()
            override_add_cuda()

    def test_extend_library_with_dispatch_key_arg(self):
        def my_sum(*args, **kwargs):
            return args[0]
        my_lib1 = Library("aten", "IMPL", dispatch_key="CPU")

        # RuntimeError: Explicitly provided dispatch key (Conjugate) is
        # inconsistent with the dispatch key of the enclosing TORCH_LIBRARY_IMPL block
        with self.assertRaisesRegex(RuntimeError, "inconsistent with the dispatch key"):
            my_lib1.impl('sum', my_sum, "Conjugate")
        my_lib1.impl('aten::sum', my_sum)
        x = torch.tensor([1, 2])
        self.assertEqual(torch.sum(x), x)
        del my_lib1

    def test_create_new_library(self) -> None:
        my_lib1 = Library("foo", "DEF")

        my_lib1.define("sum(Tensor self) -> Tensor")

        # Example 1
        @torch.library.impl(my_lib1, "sum", "CPU")
        def my_sum(*args, **kwargs):
            return args[0]

        x = torch.tensor([1, 2])
        self.assertEqual(torch.ops.foo.sum(x), x)

        my_lib2 = Library("foo", "IMPL")

        # Example 2
        @torch.library.impl(my_lib2, torch.ops.foo.sum.default, "ZeroTensor")
        def my_sum_zt(*args, **kwargs):
            if args[0]._is_zerotensor():
                return torch._efficientzerotensor(args[0].shape)
            else:
                return args[0]

        y = torch._efficientzerotensor(3)
        self.assertTrue(torch.ops.foo.sum(y)._is_zerotensor())
        self.assertEqual(torch.ops.foo.sum(x), x)

        del my_lib2
        del my_lib1

class TestPythonDispatch(TestCase):
    def test_basic(self) -> None:
        with capture_logs() as logs:
            x = LoggingTensor(torch.tensor([3.0]), requires_grad=True)
            log_input("x", x)
            y = x * x
            saved_x = y.grad_fn._saved_self
            grad_y = LoggingTensor(torch.tensor([1.0]))
            log_input("grad_y", grad_y)
            g, = torch.autograd.grad((y,), (x,), (grad_y,))

        self.assertEqual(g.elem, torch.tensor([6.0]))
        with torch.no_grad():
            self.assertEqual(saved_x, x)
            self.assertEqual(saved_x._version, x._version)
            x.add_(2)
            self.assertEqual(saved_x, x)
            # TODO: figure out why broken
            # self.assertEqual(saved_x._version, x._version)
        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = torch._ops.aten.mul.Tensor($0, $0)
$2 = input('grad_y')
$3 = torch._ops.aten.mul.Tensor($2, $0)
$4 = torch._ops.aten.mul.Tensor($2, $0)
$5 = torch._ops.aten.add.Tensor($4, $3)''')

    def test_out(self) -> None:
        with capture_logs() as logs:
            x = LoggingTensor(torch.ones(1))
            y = LoggingTensor(torch.zeros(1))
            log_input("x", x)
            log_input("y", y)
            torch.abs(x, out=y)

        self.assertEqual(y.elem, torch.ones(1))
        # TODO: arguably this shouldn't pass and we should complain
        # that out isn't a kwarg
        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = input('y')
$2 = torch._ops.aten.abs.out($0, out=$1)''')


    def test_kwarg_only(self) -> None:
        with capture_logs() as logs:
            x = LoggingTensor(torch.ones(1))
            y = LoggingTensor(torch.ones(1, 1))
            z = LoggingTensor(torch.ones(1))
            log_input("x", x)
            log_input("y", y)
            log_input("z", z)
            torch.addmv(x, y, z)
            torch.addmv(x, y, z, beta=1)
            torch.addmv(x, y, z, beta=2)
            torch.addmv(x, y, z, alpha=2)
            torch.addmv(x, y, z, beta=2, alpha=2)

        # The expectation is that beta/alpha don't show up when they're
        # defaulted.  This is even if the user explicitly specified it.
        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = input('y')
$2 = input('z')
$3 = torch._ops.aten.addmv.default($0, $1, $2)
$4 = torch._ops.aten.addmv.default($0, $1, $2)
$5 = torch._ops.aten.addmv.default($0, $1, $2, beta=2)
$6 = torch._ops.aten.addmv.default($0, $1, $2, alpha=2)
$7 = torch._ops.aten.addmv.default($0, $1, $2, beta=2, alpha=2)''')

    def test_kwarg_only_and_positional_default(self) -> None:
        with capture_logs() as logs:
            x = LoggingTensor(torch.ones(1))
            y = LoggingTensor(torch.ones(1))
            log_input("x", x)
            log_input("y", y)
            torch.ops.aten.kl_div(x, y)
            torch.ops.aten.kl_div(x, y, 2)
            torch.ops.aten.kl_div(x, y, log_target=True)
            torch.ops.aten.kl_div(x, y, 2, log_target=True)

        # What we are testing here is that we omit reduction
        # if it is defaulted, even if a kwarg is set
        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = input('y')
$2 = torch._ops.aten.kl_div.default($0, $1)
$3 = torch._ops.aten.kl_div.default($0, $1, 2)
$4 = torch._ops.aten.kl_div.default($0, $1, log_target=True)
$5 = torch._ops.aten.kl_div.default($0, $1, 2, log_target=True)''')

    def test_list_ret(self) -> None:
        # test all sequence types are permissible returns
        for list_type in (list, tuple):
            class A(torch._C._TensorBase):
                @staticmethod
                def __new__(cls, elem):
                    return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

                @classmethod
                def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                    if func.overloadpacket == torch.ops.aten.split:
                        with no_dispatch():
                            return list_type(torch.split(*args))
                    else:
                        raise AssertionError(f"unrecognized func: {func}")

            self.assertEqual(
                torch.split(A(torch.tensor([0, 1])), 2),
                torch.split(torch.tensor([0, 1]), 2)
            )

    def test_invalid_ret(self) -> None:
        # test invalid return gets reasonable error message
        class A(torch._C._TensorBase):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                return "arf"

        # Wobbles depending on NDEBUG mode of pybind11
        self.assertRaisesRegex(
            RuntimeError, "Unable to cast", lambda: A(torch.zeros(1)).neg(),
        )
        self.assertRaisesRegexp(
            RuntimeError, "Unable to cast", lambda: A(torch.zeros(1)).detach(),
        )

    def test_detach_appears_twice_when_called_once(self) -> None:
        with capture_logs() as logs:
            x = LoggingTensor(torch.tensor([3.0]), requires_grad=True)
            log_input("x", x)
            x.detach()
        # FIXME: We actually want this to emit a single detach. However,
        # it currently emits two, for reasons unclear to us. Leaving
        # this test here to make sure we don't regress even further (it
        # would be bad if calling .detach() once emits 3+ detaches).
        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = torch._ops.aten.detach.default($0)
$2 = torch._ops.aten.detach.default($1)''')

    def test_metadata_change_not_allowed(self) -> None:
        x = LoggingTensor(torch.ones(1))
        y = x.data
        self.assertIsInstance(y, LoggingTensor)
        self.assertRaises(RuntimeError, lambda: y.resize_(4))

    def test_storage(self) -> None:
        # For now, just make sure it doesn't crash.  Ideally, we should
        # return some virtual storage that is safe to work with
        x = LoggingTensor(torch.ones(1))
        self.assertRaises(RuntimeError, lambda: x.storage())

    def test_make_wrapper_subclass_noalloc(self) -> None:
        # This is ludicrously big (8TB) and this should pass because wrapper
        # subclasses don't allocate
        torch.Tensor._make_wrapper_subclass(LoggingTensor, (1000000000000,))

    def test_version(self) -> None:
        x = LoggingTensor(torch.ones(1))
        prev_vc = x._version
        x.detach().add_(2)
        cur_vc = x._version
        self.assertNotEqual(prev_vc, cur_vc)
        x.data.add_(2)
        self.assertEqual(cur_vc, x._version)

    def test_subclass_priority(self) -> None:
        class ErrorA(RuntimeError):
            pass

        class ErrorB(RuntimeError):
            pass

        # The big tests for code coverage are test_precedence_semantics in
        # test_overrides.py; this is just to make sure it is wired up at all
        # correctly for __torch_dispatch__
        class A(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                raise ErrorA

        class B(A):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                raise ErrorB

        self.assertRaises(ErrorA, lambda: torch.add(A(torch.empty(1)), A(torch.empty(1))))
        self.assertRaises(ErrorB, lambda: torch.add(A(torch.empty(1)), B(torch.empty(1))))
        self.assertRaises(ErrorB, lambda: torch.add(B(torch.empty(1)), A(torch.empty(1))))
        self.assertRaises(ErrorB, lambda: torch.add(B(torch.empty(1)), B(torch.empty(1))))

    def test_format(self) -> None:
        x = LoggingTensor(torch.ones(1))
        s1 = str(x)
        s2 = repr(x)
        s3 = f"{x}"
        self.assertExpectedInline(s1, """LoggingTensor(tensor([1.]))""")
        self.assertEqual(s1, s2)
        self.assertEqual(s1, s3)

    def test_custom_autograd(self) -> None:
        escape = [None]

        class Square(torch.autograd.Function):
            @staticmethod
            def forward(ctx, x):
                y = x ** 2
                ctx.save_for_backward(x)
                return y

            @staticmethod
            def backward(ctx, grad_output):
                assert isinstance(grad_output, LoggingTensor)
                x, = ctx.saved_tensors
                assert isinstance(x, LoggingTensor)
                escape[0] = x
                return grad_output * 2 * x

        with capture_logs() as logs:
            x = LoggingTensor(torch.ones(1), requires_grad=True)
            log_input("x", x)
            x.grad = LoggingTensor(torch.zeros(1))
            log_input("x.grad", x.grad)
            y = Square.apply(x)
            grad_output = LoggingTensor(torch.ones(1))
            log_input("grad_output", grad_output)
            y.backward(grad_output)

        with torch.no_grad():
            self.assertEqual(escape[0], x)
            self.assertEqual(escape[0]._version, x._version)
            # TODO: figure out why x.requires_grad = False doesn't
            # trigger an error for LoggingTensor
            x.add_(2)
            self.assertEqual(escape[0], x)
            # TODO: figure out why this is broken
            # self.assertEqual(escape[0]._version, x._version)

        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = input('x.grad')
$2 = torch._ops.aten.pow.Tensor_Scalar($0, 2)
$3 = input('grad_output')
$4 = torch._ops.aten.mul.Tensor($3, 2)
$5 = torch._ops.aten.mul.Tensor($4, $0)
$6 = torch._ops.aten.add_.Tensor($1, $5)''')

    def test_subclass_creation(self):
        # Make sure these statements runs without error
        # In particular checking that when internal detach returns
        # subclasses, these are cleanly overwritten.
        class Foo(torch.Tensor):
            pass

        err_msg = "subclass Foo but.*already associated to a python object of type LoggingTensor"
        with self.assertRaisesRegex(RuntimeError, err_msg):
            a = torch.Tensor._make_subclass(Foo, LoggingTensor(torch.rand(2)))
        with self.assertRaisesRegex(RuntimeError, err_msg):
            b = LoggingTensor(torch.rand(2)).as_subclass(Foo)
        with self.assertRaisesRegex(RuntimeError, err_msg):
            Foo(LoggingTensor(torch.rand(2)))

        with self.assertRaisesRegex(TypeError, "Foo must define __torch_dispatch__"):
            torch.Tensor._make_wrapper_subclass(Foo, (2, 2))

    def test_new_ones(self) -> None:
        class MyTensor(torch.Tensor):
            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                return MyTensor(3)

        self.assertEqual(type(MyTensor(2).new_ones(3)), MyTensor)

    def test_like(self) -> None:
        class MyTensor(torch.Tensor):
            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                return MyTensor(3)

        for f in ["empty", "ones", "rand", "randn", "zeros"]:
            f_name = f + "_like"
            self.assertEqual(type(getattr(torch, f_name)(MyTensor(2))), MyTensor)

        self.assertEqual(type(torch.full_like(MyTensor(2), 1.)), MyTensor)
        self.assertEqual(type(torch.randint_like(MyTensor(2), high=3)), MyTensor)

    def test_make_wrapper_subclass_propagates_metadata(self) -> None:
        class WrapperTensor(torch.Tensor):
            elem: torch.Tensor

            __slots__ = ['elem']

            @staticmethod
            def __new__(cls, elem, *args, **kwargs):
                r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
                    cls, elem.size(),
                    dtype=elem.dtype, layout=elem.layout,
                    device=elem.device, requires_grad=elem.requires_grad,
                    strides=elem.stride(), storage_offset=elem.storage_offset())
                r.elem = elem
                return r

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                raise RuntimeError("NYI")

        # non-contiguous strides, non-zero storage offset
        x = torch.randn(4, 6).t().diagonal(offset=2)
        y = WrapperTensor(x)
        self.assertEqual(y.size(), x.size())
        self.assertEqual(y.stride(), x.stride())
        self.assertEqual(y.storage_offset(), x.storage_offset())

    def test_wrapper_subclass_serializes(self) -> None:
        with tempfile.TemporaryFile() as f:
            x = LoggingTensor(torch.randn(3))
            torch.save(x, f)
            f.seek(0)
            x_loaded = torch.load(f)
            self.assertTrue(type(x_loaded) is type(x))
            self.assertEqual(x.elem, x_loaded.elem)
            self.assertFalse(x is x_loaded)

    def test_deepcopy_wrapper_subclass(self) -> None:
        x = LoggingTensor(torch.randn(3))
        x_copy = deepcopy(x)
        self.assertTrue(type(x_copy) is type(x))
        self.assertEqual(x.elem, x_copy.elem)
        self.assertFalse(x is x_copy)

    def test_deepcopy_wrapper_subclass_with_clone_returning_different_type(self) -> None:

        class MyWrapperTensor(torch.Tensor):
            elem: torch.Tensor

            __slots__ = ['elem']

            @staticmethod
            def __new__(cls, elem, *args, **kwargs):
                r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
                    cls, elem.size(),
                    dtype=elem.dtype, layout=elem.layout,
                    device=elem.device, requires_grad=elem.requires_grad,
                    strides=elem.stride(), storage_offset=elem.storage_offset())
                r.elem = elem
                return r

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                if func.overloadpacket.__name__ == "clone":
                    # Return a plain tensor from clone().
                    return args[0].elem.clone()
                raise RuntimeError("NYI")

            # NB: The default Tensor.__torch_function__ implementation called for deepcopy
            # disables __torch_function__ by the time we get to clone(), so there is no need to
            # explicitly disable __torch_function__ for this subclass.

        x = MyWrapperTensor(torch.randn(3))
        with self.assertRaisesRegex(RuntimeError,
                                    "for which cloning returns another instance of the same subclass"):
            x_copy = deepcopy(x)

    def test_deepcopy_non_wrapper_subclass(self) -> None:

        # Ensure correct error is thrown for common error cases.
        class SubTensorError1(torch.Tensor):
            # Default implementation of new_empty() returns a plain tensor.
            pass

        class SubTensorError2(torch.Tensor):
            # new_empty() incorrectly returns a different type (i.e. a plain tensor).
            def new_empty(self, shape):
                return torch.Tensor(shape)

        for error_cls in [SubTensorError1, SubTensorError2]:
            x = error_cls(3)
            with self.assertRaisesRegex(RuntimeError,
                                        "for which that function returns another instance of the same subclass"):
                x_copy = deepcopy(x)

        # Ensure a correctly implemented new_empty() causes deepcopy() to work.
        class SubTensorSuccess(torch.Tensor):
            def new_empty(self, shape):
                return type(self)(shape)

        x = SubTensorSuccess(3)
        x_copy = deepcopy(x)
        self.assertIs(type(x_copy), type(x))

    def test_index_put_where_only_index_is_subclass(self) -> None:
        called_funcs = []

        class MyTensor(torch.Tensor):
            __torch_function__ = torch._C._disabled_torch_function_impl
            elem: torch.Tensor
            __slots__ = ['elem']

            @staticmethod
            def __new__(cls, elem, *args, **kwargs):
                r = torch.Tensor._make_wrapper_subclass(
                    cls, elem.size(),
                    dtype=elem.dtype, layout=elem.layout,
                    device=elem.device, requires_grad=elem.requires_grad
                )
                r.elem = elem
                return r

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                called_funcs.append(func)
                return MyTensor(torch.tensor(3))

        x = torch.randn(3, 3)
        idxs = (MyTensor(torch.tensor(0)),)
        v = torch.randn(1)
        res = x.index_put_(idxs, v)
        self.assertEqual(called_funcs, [torch.ops.aten.index_put_.default])

    def test_enable_torch_dispatch_mode_error(self) -> None:
        z = LoggingTensor(torch.empty([]))
        with self.assertRaisesRegex(ValueError, "expected to get TorchDispatchMode, Tensor-like class, or None"):
            with enable_torch_dispatch_mode(z):
                pass

    def test_enable_torch_dispatch_mode_basic(self) -> None:
        with enable_torch_dispatch_mode(LoggingTensorMode):
            z = torch.empty([])
            self.assertTrue(isinstance(z, LoggingTensorMode))

    def test_enable_torch_dispatch_mode_unrelated_tensors(self) -> None:
        x = torch.randn([])
        y = torch.randn([])
        with enable_torch_dispatch_mode(LoggingTensorMode):
            z = x + y
            self.assertTrue(isinstance(z, LoggingTensorMode))

    def test_enable_torch_dispatch_mode_subclass_priority(self) -> None:
        class ErrorA(RuntimeError):
            pass

        class ErrorB(RuntimeError):
            pass

        class A(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                raise ErrorA

        class B(A):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                raise ErrorB

        a = A(torch.empty(1))
        b = B(torch.empty(1))
        with self.assertRaises(ErrorA):
            a + a
        with self.assertRaises(ErrorB):
            a + b

        # B has precedence over A due to the subclass relationship yet
        # modes take precedence over arguments
        with self.assertRaises(ErrorA):
            with enable_torch_dispatch_mode(A):
                b + b
        with self.assertRaises(ErrorB):
            with enable_torch_dispatch_mode(B):
                a + a
        with self.assertRaises(ErrorB):
            with enable_torch_dispatch_mode(B):
                a + b

    def test_enable_torch_dispatch_mode_respects_no_dispatch(self) -> None:
        with enable_torch_dispatch_mode(LoggingTensorMode):
            z = torch.ones([2, 3])
            self.assertTrue(isinstance(z, LoggingTensorMode))
            with no_dispatch():
                expected = torch.ones([2, 3])
                self.assertEqual(z.elem, expected)

    def test_enable_torch_dispatch_mode_instance(self) -> None:
        class TestMode(TorchDispatchMode):
            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                if kwargs is None:
                    kwargs = {}
                return func(*args, **kwargs)

        x = TestMode(inner=None)
        y = torch.tensor([2.])
        with enable_torch_dispatch_mode(x):
            y + y

    def test_nested_enable_torch_dispatch_mode(self) -> None:
        class A(LoggingTensorMode):
            pass

        with self.assertRaisesRegex(ValueError, "there is already an active mode"):
            with enable_torch_dispatch_mode(LoggingTensorMode):
                with enable_torch_dispatch_mode(A):
                    pass

    def test_nesting_with_same_enable_torch_dispatch_mode(self) -> None:
        # "nested" enable_torch_dispatch_modes are allowed if they're the same mode. It's the equivalent of
        # a noop, so it will only write once to the log
        with capture_logs() as logs:
            x = LoggingTensor(torch.tensor([3.]))
            log_input("x", x)
            with enable_torch_dispatch_mode(LoggingTensor):
                with enable_torch_dispatch_mode(LoggingTensor):
                    x + x

        self.assertExpectedInline('\n'.join(logs), '''\
$0 = input('x')
$1 = torch._ops.aten.add.Tensor($0, $0)''')

    def test_enable_torch_dispatch_mode_ignore_preexisting(self):
        class A(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                return cls(torch.zeros(()))

        class B(A):
            pass

        with enable_torch_dispatch_mode(A):
            with enable_torch_dispatch_mode(B, ignore_preexisting=True):
                self.assertTrue(isinstance(torch.zeros(()), B))

    def test_enable_torch_dispatch_mode_replace(self):
        class A(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                return cls(torch.zeros(()))

        class B(A):
            pass

        with enable_torch_dispatch_mode(A):
            with enable_torch_dispatch_mode(B, replace=A):
                self.assertTrue(isinstance(torch.zeros(()), B))

    def test_exception_handling(self):
        class A(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                if func.__name__ == 'randn.default':
                    raise RuntimeError()
                return cls(torch.zeros(()))

        with enable_torch_dispatch_mode(A):
            try:
                torch.randn(())
            except RuntimeError:
                pass
            self.assertTrue(isinstance(torch.zeros(()), A))

    def test_push_torch_dispatch_mode(self) -> None:
        class ErrorA(RuntimeError):
            def __init__(self, msg=None):
                return super().__init__(msg)

        class A(TorchDispatchMode):
            def __init__(self, msg=None):
                self.msg = msg

            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                raise ErrorA(self.msg)

        x = torch.randn(3)
        with self.assertRaises(ErrorA):
            with push_torch_dispatch_mode(A):
                torch.add(x, x)

        with self.assertRaisesRegex(ErrorA, r"partial constructor"):
            with push_torch_dispatch_mode(partial(A, "partial constructor")):
                x + x

    def test_torch_dispatch_mode_stack(self) -> None:
        logs = []

        class Logger(TorchDispatchMode):
            def __init__(self, name):
                self.name = name

            def __torch_dispatch__(self, func, types, args=(), kwargs=None):
                if kwargs is None:
                    kwargs = {}
                logs.append(self.name)
                return func(*args, **kwargs)

        x = torch.randn(1)
        with push_torch_dispatch_mode(partial(Logger, "A")):
            with push_torch_dispatch_mode(partial(Logger, "B")):
                x + x
        self.assertEqual(logs, ["B", "A"])

    def test_push_mode_instance_errors(self):
        class A(TorchDispatchMode):
            pass
        with self.assertRaisesRegex(ValueError, 'instance of TorchDispatchMode'):
            with push_torch_dispatch_mode(A(inner=None)):
                pass

    def test_push_mode_returns_unrelated(self):
        with self.assertRaisesRegex(ValueError, 'return a TorchDispatchMode'):
            with push_torch_dispatch_mode(lambda *, inner: None):
                pass

    def test_missing_inner_mode_ctor(self):
        self.assertRaisesRegex(TypeError, 'push_torch_dispatch_mode', lambda: TorchDispatchMode())

    def test_tolist_numpy_with_torch_dispatch_mode(self) -> None:
        x = LoggingTensor(torch.tensor([2.0, 3.0]))
        with self.assertRaisesRegex(RuntimeError, "is not supported for tensor subclasses."):
            x.tolist()
        with self.assertRaisesRegex(RuntimeError, "is not supported for tensor subclasses."):
            x.numpy()
        with self.assertRaises(AssertionError):
            self.assertEqual(x, None)

    def test_enable_torch_dispatch_mode_subclass_autograd_device_check(self) -> None:
        class NonWrapperSubclass(torch.Tensor):
            elem: torch.Tensor

            __slots__ = ['elem']

            @staticmethod
            def __new__(cls, elem, *args, **kwargs):
                # Wrong device here!
                r = torch.Tensor._make_subclass(cls, elem.to("meta"), elem.requires_grad)
                # ...the real tensor is held as an element on the tensor.
                r.elem = elem
                return r

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                def unwrap(e):
                    return e.elem if isinstance(e, NonWrapperSubclass) else e

                def wrap(e):
                    return NonWrapperSubclass(e) if isinstance(e, torch.Tensor) else e

                rs = tree_map(wrap, func(*tree_map(unwrap, args), **tree_map(unwrap, kwargs)))
                logging.getLogger("NonWrapperSubclass").info(f"{func.__module__}.{func.__name__}", args, kwargs, rs)
                return rs

        x = NonWrapperSubclass(torch.tensor([3.0, 4.0], requires_grad=True))
        y = torch.randn(2, requires_grad=True)
        z = x * y
        self.assertIsInstance(z, NonWrapperSubclass)
        z.sum().backward(torch.tensor(1))
        self.assertEqual(x.grad, y)
        self.assertEqual(y.grad, x)

    def test_none_wrapping(self):
        # A Tensor subclass that returns None when doing add
        # See LoggingTensor above for more details on the subclass
        class SubclassWithNone(torch.Tensor):
            @staticmethod
            def __new__(cls, elem, *args, **kwargs):
                r = torch.Tensor._make_wrapper_subclass(
                    cls, elem.size(),
                    dtype=elem.dtype, layout=elem.layout,
                    device=elem.device, requires_grad=elem.requires_grad
                )
                r.elem = elem
                return r

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                def unwrap(e):
                    return e.elem if isinstance(e, SubclassWithNone) else e

                def wrap(e):
                    return SubclassWithNone(e) if isinstance(e, torch.Tensor) else e

                rs = tree_map(wrap, func(*tree_map(unwrap, args), **tree_map(unwrap, kwargs)))
                if func.overloadpacket.__name__ == "add":
                    return None
                else:
                    return rs

        x = SubclassWithNone(torch.rand(2))
        # Make sure both run without error
        self.assertIsInstance(x * 2, SubclassWithNone)
        self.assertIsNone(x + 2)

        x.requires_grad_()
        out = x.acos().sum()

        # The backward of acos does add then rsqrt so here we make sure that the
        # undefined Tensor generated by the user code is nicely handled.
        # If acos formula changes in the future, this can be replaced by any other
        # function that does add then something in the backward in a composite way
        with self.assertRaisesRegex(RuntimeError, "but got None"):
            out.backward()

    def test_storage_can_be_converted_to_python_object(self):
        with enable_torch_dispatch_mode(LoggingTensorMode):
            s = torch.Storage()
            z = LoggingTensorMode(torch.empty([]))
            z.set_(s)

    def test_autograd_in_attr(self):
        # We want the wrapped Tensor to require gradients!
        true_t = torch.rand(2, requires_grad=True)
        t = LoggingTensorReentrant(true_t)

        out = t + 2

        self.assertFalse(out.requires_grad)
        self.assertIsNone(out.grad_fn)

        self.assertTrue(out.elem.requires_grad)
        self.assertIsNotNone(out.elem.grad_fn)

        with self.assertRaisesRegex(RuntimeError, "does not require grad"):
            out.sum().backward()

        out.elem.sum().backward()

        self.assertIsNone(t.grad)
        self.assertIsNotNone(t.elem.grad)

    def test_dispatch_super_call(self):
        called = []

        class SubTensor(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem)

            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                called.append(func)
                return super().__torch_dispatch__(func, types, args, kwargs)

        x = torch.randn(2)
        y = torch.randn(2)
        self.assertEqual(SubTensor(x) + SubTensor(y), x + y)
        self.assertEqual(called, [torch.ops.aten.add.Tensor])

    def test_dispatch_super_call_list_arg(self):
        called = []

        class SubTensorWithListArg(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem)

            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                called.append(func)
                return super().__torch_dispatch__(func, types, list(args), kwargs)

        x = torch.randn(2)
        self.assertEqual(SubTensorWithListArg(x).neg(), x.neg())
        self.assertEqual(called, [torch.ops.aten.neg.default])

    def test_dispatch_super_dont_autograd(self):
        called = []

        class SubTensor(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                return torch.Tensor._make_subclass(cls, elem, elem.requires_grad)

            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                called.append(func)
                # This argument still requires grad because it was passed
                # through directly...
                self.assertTrue(args[0].requires_grad)
                r = super().__torch_dispatch__(func, types, args, kwargs)
                # But the output better not require grad, because that means
                # you did autograd again in torch dispatch (oops)
                self.assertFalse(r.requires_grad)
                return r

        x = SubTensor(torch.randn(2, requires_grad=True))
        x.neg()
        self.assertEqual(called, [torch.ops.aten.neg.default])

    def test_set_data(self):
        called = 0

        class SubTensor(torch.Tensor):
            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                nonlocal called
                called += 1
                return super().__torch_dispatch__(func, types, args, kwargs)

        x = SubTensor(torch.empty(2))
        x.data
        self.assertEqual(called, 1)
        x.data = torch.empty(2)
        self.assertEqual(called, 1)
        x.data
        self.assertEqual(called, 2)
        self.assertIs(type(x), SubTensor)
        x.set_(torch.empty(2))
        self.assertEqual(called, 3)
        x.data
        self.assertEqual(called, 4)
        self.assertIs(type(x), SubTensor)

    def test_construct_int_tensor(self):
        class SubTensor(torch.Tensor):
            pass
        # should not fail
        SubTensor(torch.zeros(2, dtype=torch.int))

    def test_multiple_ops_subclass(self):
        # This is a Direct Subclass, don't do that!
        class MySubclass(torch.Tensor):
            @staticmethod
            def __new__(cls, elem):
                r = torch.Tensor._make_subclass(cls, elem)
                return r

            __torch_function__ = torch._C._disabled_torch_function_impl

            @classmethod
            def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
                with no_dispatch():
                    return func(*args, **kwargs)

        x = MySubclass(torch.rand(2, 2, dtype=torch.complex64))
        y = x.conj()
        # Details of the bug that this tests for:
        # Here, y dispatch keys are: {PythonTLSSnapshot, AutogradCPU, Conjugate, Python, CPU}
        # There are a few calls to the dispatcher that are going to happen here:
        #  - call_exp: User calling exp on y
        #    - PythonTLSSnapshot: records the TLS on entry and redispatch
        #    - AutogradCPU: no input requires grad, so does nothing and redispatch
        #    - Conjugate: no special implementation for exp: use the fallback that
        #                 first clone the Tensor (to materialize the conj) then redispatch
        #      - call_clone: conjugate fallback calling clone on y
        #        - PythonTLSSnapshot: records the TLS on entry and redispatch
        #        - (AutogradCPU: skipped as autograd added itself to the exclude set above)
        #        - Conjugate: special implementation for clone: just skip this key
        #        - Python: Reset the TLS based on the snapshot above and call the user implementation (this
        #                  actually calls into the dispatcher again but since we disable both our keys
        #                  before, not detailed here)
        #        - exit Python: restore the TLS and exit
        #        - exit Conjugate: nothing was inplace so just exit
        #        - exit PythonTLSSnapshot: done with this call, reset the saved TLS to empty
        #    - Python: Reset the TLS again based on the snapshot. <- this used to fail
        #    - More steps....
        y.exp()



if __name__ == '__main__':
    run_tests()

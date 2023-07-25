from __future__ import annotations

import math
import operator
from functools import partial, reduce, singledispatch

import datafusion as df
import datafusion.functions
import pyarrow as pa
import pyarrow.compute as pc

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
from ibis.expr.operations.udf import InputType
from ibis.formats.pyarrow import PyArrowType


@singledispatch
def translate(expr, **_):
    raise NotImplementedError(expr)


@translate.register(ops.Node)
def operation(op, **_):
    raise com.OperationNotDefinedError(f'No translation rule for {type(op)}')


@translate.register(ops.DatabaseTable)
def table(op, ctx, **_):
    return ctx.table(op.name)


@translate.register(ops.DummyTable)
def dummy_table(op, ctx, **kw):
    return ctx.empty_table().select(
        *(
            translate(ops.Cast(value, to=dtype), ctx=ctx, **kw)
            for value, dtype in zip(op.values, op.schema.types)
        )
    )


@translate.register(ops.InMemoryTable)
def in_memory_table(op, ctx, **kw):
    schema = op.schema

    if data := op.data:
        return ctx.from_arrow_table(data.to_pyarrow(schema), name=op.name)

    # datafusion panics when given an empty table
    return (
        ctx.empty_table()
        .select(
            *(
                translate(
                    ops.Alias(ops.Literal(None, dtype=dtype), name), ctx=ctx, **kw
                )
                for name, dtype in schema.items()
            )
        )
        .limit(0)
    )


@translate.register(ops.Alias)
def alias(op, **kw):
    arg = translate(op.arg, **kw)
    return arg.alias(op.name)


@translate.register(ops.Literal)
def literal(op, **_):
    if isinstance(op.value, (set, frozenset)):
        value = list(op.value)
    else:
        value = op.value

    arrow_type = PyArrowType.from_ibis(op.dtype)
    arrow_scalar = pa.scalar(value, type=arrow_type)

    return df.literal(arrow_scalar)


@translate.register(ops.Cast)
def cast(op, **kw):
    arg = translate(op.arg, **kw)
    typ = PyArrowType.from_ibis(op.to)
    return arg.cast(to=typ)


@translate.register(ops.TableColumn)
def column(op, **_):
    id_parts = [getattr(op.table, "name", None), op.name]
    return df.column(".".join(f'"{id}"' for id in id_parts if id))


@translate.register(ops.SortKey)
def sort_key(op, **kw):
    arg = translate(op.expr, **kw)
    return arg.sort(ascending=op.ascending)


@translate.register(ops.Selection)
def selection(op, **kw):
    plan = translate(op.table, **kw)

    if op.predicates:
        predicates = map(partial(translate, **kw), op.predicates)
        predicate = reduce(operator.and_, predicates)
        plan = plan.filter(predicate)

    selections = []
    for arg in op.selections or [op.table]:
        # TODO(kszucs) it would be nice if we wouldn't need to handle the
        # specific cases in the backend implementations, we could add a
        # new operator which retrieves all of the Table columns
        # (.e.g. Asterisk) so the translate() would handle this
        # automatically
        if isinstance(arg, ops.TableNode):
            for name in arg.schema.names:
                column = ops.TableColumn(table=arg, name=name)
                field = translate(column, **kw)
                selections.append(field)
        elif isinstance(arg, ops.Value):
            field = translate(arg, **kw)
            selections.append(field)
        else:
            raise com.TranslationError(
                "DataFusion backend is unable to compile selection with "
                f"operation type of {type(arg)}"
            )

    plan = plan.select(*selections)

    if op.sort_keys:
        sort_keys = map(partial(translate, **kw), op.sort_keys)
        plan = plan.sort(*sort_keys)

    return plan


@translate.register(ops.Limit)
def limit(op, **kw):
    if op.offset:
        raise NotImplementedError("DataFusion does not support offset")
    return translate(op.table, **kw).limit(op.n)


@translate.register(ops.Aggregation)
def aggregation(op, **kw):
    table = translate(op.table, **kw)
    group_by = [translate(arg, **kw) for arg in op.by]
    metrics = [translate(arg, **kw) for arg in op.metrics]

    if op.predicates:
        table = table.filter(
            reduce(operator.and_, map(partial(translate, **kw), op.predicates))
        )

    return table.aggregate(group_by, metrics)


@translate.register(ops.Not)
def invert(op, **kw):
    return ~translate(op.arg, **kw)


@translate.register(ops.And)
def and_(op, **kw):
    left = translate(op.left, **kw)
    right = translate(op.right, **kw)
    return left & right


@translate.register(ops.Or)
def or_(op, **kw):
    left = translate(op.left, **kw)
    right = translate(op.right, **kw)
    return left | right


@translate.register(ops.Abs)
def abs(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.abs(arg)


@translate.register(ops.Ceil)
def ceil(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.ceil(arg).cast(pa.int64())


@translate.register(ops.Floor)
def floor(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.floor(arg).cast(pa.int64())


@translate.register(ops.Round)
def round(op, **kw):
    arg = translate(op.arg, **kw)
    if op.digits is not None:
        raise com.UnsupportedOperationError(
            'Rounding to specific digits is not supported in datafusion'
        )
    return df.functions.round(arg).cast(pa.int64())


@translate.register(ops.Ln)
def ln(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.ln(arg)


@translate.register(ops.Log2)
def log2(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.log2(arg)


@translate.register(ops.Log10)
def log10(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.log10(arg)


@translate.register(ops.Sqrt)
def sqrt(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.sqrt(arg)


@translate.register(ops.Strip)
def strip(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.trim(arg)


@translate.register(ops.LStrip)
def lstrip(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.ltrim(arg)


@translate.register(ops.RStrip)
def rstrip(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.rtrim(arg)


@translate.register(ops.Lowercase)
def lower(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.lower(arg)


@translate.register(ops.Uppercase)
def upper(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.upper(arg)


@translate.register(ops.Reverse)
def reverse(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.reverse(arg)


@translate.register(ops.StringLength)
def strlen(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.character_length(arg)


@translate.register(ops.Capitalize)
def capitalize(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.initcap(arg)


@translate.register(ops.Substring)
def substring(op, **kw):
    arg = translate(op.arg, **kw)
    start = translate(ops.Add(left=op.start, right=1))
    if op_length := op.length:
        length = translate(op_length, **kw)
        return df.functions.substr(arg, start, length)
    else:
        return df.functions.substr(arg, start)


@translate.register(ops.Repeat)
def repeat(op, **kw):
    arg = translate(op.arg, **kw)
    times = translate(op.times, **kw)
    return df.functions.repeat(arg, times)


@translate.register(ops.LPad)
def lpad(op, **kw):
    arg = translate(op.arg, **kw)
    length = translate(op.length, **kw)
    pad = translate(op.pad, **kw)
    return df.functions.lpad(arg, length, pad)


@translate.register(ops.RPad)
def rpad(op, **kw):
    arg = translate(op.arg, **kw)
    length = translate(op.length, **kw)
    pad = translate(op.pad, **kw)
    return df.functions.rpad(arg, length, pad)


@translate.register(ops.GreaterEqual)
def ge(op, **kw):
    return translate(op.left, **kw) >= translate(op.right, **kw)


@translate.register(ops.LessEqual)
def le(op, **kw):
    return translate(op.left, **kw) <= translate(op.right, **kw)


@translate.register(ops.Greater)
def gt(op, **kw):
    return translate(op.left, **kw) > translate(op.right, **kw)


@translate.register(ops.Less)
def lt(op, **kw):
    return translate(op.left, **kw) < translate(op.right, **kw)


@translate.register(ops.Equals)
def eq(op, **kw):
    return translate(op.left, **kw) == translate(op.right, **kw)


@translate.register(ops.NotEquals)
def ne(op, **kw):
    return translate(op.left, **kw) != translate(op.right, **kw)


@translate.register(ops.Add)
def add(op, **kw):
    return translate(op.left, **kw) + translate(op.right, **kw)


@translate.register(ops.Subtract)
def sub(op, **kw):
    return translate(op.left, **kw) - translate(op.right, **kw)


@translate.register(ops.Multiply)
def mul(op, **kw):
    return translate(op.left, **kw) * translate(op.right, **kw)


@translate.register(ops.Divide)
def div(op, **kw):
    return translate(op.left, **kw) / translate(op.right, **kw)


@translate.register(ops.FloorDivide)
def floordiv(op, **kw):
    return df.functions.floor(translate(op.left, **kw) / translate(op.right, **kw))


@translate.register(ops.Modulus)
def mod(op, **kw):
    return translate(op.left, **kw) % translate(op.right, **kw)


@translate.register(ops.Count)
def count(op, **kw):
    return df.functions.count(translate(op.arg, **kw))


@translate.register(ops.CountDistinct)
def count_distinct(op, **kw):
    return df.functions.count(translate(op.arg, **kw), distinct=True)


@translate.register(ops.CountStar)
def count_star(_, **__):
    return df.functions.count(df.literal(1))


@translate.register(ops.Sum)
def sum(op, **kw):
    arg = translate(op.arg, **kw)
    if op.arg.output_dtype.is_boolean():
        arg = arg.cast(pa.int64())
    return df.functions.sum(arg)


@translate.register(ops.Min)
def min(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.min(arg)


@translate.register(ops.Max)
def max(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.max(arg)


@translate.register(ops.Mean)
def mean(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.avg(arg)


@translate.register(ops.Median)
def median(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.median(arg)


@translate.register(ops.ApproxMedian)
def approx_median(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.approx_median(arg)


@translate.register(ops.Variance)
def variance(op, **kw):
    arg = translate(op.arg, **kw)

    if op.how == "sample":
        return df.functions.var_samp(arg)
    elif op.how == "pop":
        return df.functions.var_pop(arg)
    else:
        raise ValueError(f"Unrecognized how value: {op.how}")


@translate.register(ops.StandardDev)
def stddev(op, **kw):
    arg = translate(op.arg, **kw)

    if op.how == "sample":
        return df.functions.stddev_samp(arg)
    elif op.how == "pop":
        return df.functions.stddev_pop(arg)
    else:
        raise ValueError(f"Unrecognized how value: {op.how}")


@translate.register(ops.Contains)
def contains(op, **kw):
    value = translate(op.value, **kw)
    options = list(map(partial(translate, **kw), op.options))
    return df.functions.in_list(value, options, negated=False)


@translate.register(ops.NotContains)
def not_contains(op, **kw):
    value = translate(op.value, **kw)
    options = list(map(partial(translate, **kw), op.options))
    return df.functions.in_list(value, options, negated=True)


@translate.register(ops.Negate)
def negate(op, **kw):
    return df.lit(-1) * translate(op.arg, **kw)


@translate.register(ops.Acos)
@translate.register(ops.Asin)
@translate.register(ops.Atan)
@translate.register(ops.Cos)
@translate.register(ops.Sin)
@translate.register(ops.Tan)
@translate.register(ops.Exp)
def trig(op, **kw):
    func_name = op.__class__.__name__.lower()
    func = getattr(df.functions, func_name)
    return func(translate(op.arg, **kw))


@translate.register(ops.Atan2)
def atan2(op, **kw):
    y, x = map(partial(translate, **kw), op.args)
    return df.functions.atan(y / x)


@translate.register(ops.Cot)
def cot(op, **kw):
    x = translate(op.arg, **kw)
    return df.lit(1.0) / df.functions.tan(x)


@translate.register(ops.Radians)
def radians(op, **kw):
    return translate(op.arg, **kw) * df.lit(math.pi) / df.lit(180)


@translate.register(ops.Degrees)
def degrees(op, **kw):
    return translate(op.arg, **kw) * df.lit(180) / df.lit(math.pi)


@translate.register(ops.Power)
def power(op, **kw):
    base = translate(op.left, **kw)
    exponent = translate(op.right, **kw)
    return df.functions.power(base, exponent)


@translate.register(ops.Sign)
def sign(op, **kw):
    arg = translate(op.arg, **kw)

    arrow_sign = df.udf(
        pc.sign,
        input_types=[PyArrowType.from_ibis(op.arg.output_dtype)],
        return_type=PyArrowType.from_ibis(op.output_dtype),
        volatility="immutable",
    )

    return arrow_sign(arg)


@translate.register(ops.NullIfZero)
def null_if_zero(op, **kw):
    arg = translate(op.arg, **kw)
    return df.functions.nullif(arg, df.literal(0))


@translate.register(ops.Log)
def log(op, **kw):
    arg = translate(op.arg, **kw)
    base = translate(op.base, **kw)
    return df.functions.log(base, arg)


@translate.register(ops.RandomScalar)
def random_scalar(_, **__):
    return df.functions.random()


@translate.register(ops.Pi)
def pi(_, **__):
    return df.lit(math.pi)


@translate.register(ops.E)
def e(_, **__):
    return df.lit(math.e)


@translate.register(ops.ElementWiseVectorizedUDF)
def elementwise_udf(op, **kw):
    udf = df.udf(
        op.func,
        input_types=list(map(PyArrowType.from_ibis, op.input_type)),
        return_type=PyArrowType.from_ibis(op.return_type),
        volatility="volatile",
    )
    args = map(partial(translate, **kw), op.func_args)

    return udf(*args)


@translate.register(ops.ScalarUDF)
def scalar_udf(op, **kw):
    if (input_type := op.__input_type__) != InputType.PYARROW:
        raise NotImplementedError(
            f"DataFusion only supports pyarrow UDFs: got a {input_type.name.lower()} UDF"
        )
    udf = df.udf(
        op.__func__,
        input_types=[PyArrowType.from_ibis(arg.output_dtype) for arg in op.args],
        return_type=PyArrowType.from_ibis(op.output_dtype),
        volatility="volatile",
    )
    args = map(partial(translate, **kw), op.args)

    return udf(*args)


@translate.register(ops.StringConcat)
def string_concat(op, **kw):
    return df.functions.concat(*map(partial(translate, **kw), op.arg))


@translate.register(ops.Translate)
def string_translate(op, **kw):
    return df.functions.translate(*map(partial(translate, **kw), op.args))


@translate.register(ops.StringAscii)
def string_ascii(op, **kw):
    return df.functions.ascii(translate(op.arg, **kw))


@translate.register(ops.StartsWith)
def string_starts_with(op, **kw):
    return df.functions.starts_with(translate(op.arg, **kw), translate(op.start, **kw))


@translate.register(ops.StrRight)
def string_right(op, **kw):
    return df.functions.right(translate(op.arg, **kw), translate(op.nchars, **kw))


@translate.register(ops.RegexExtract)
def regex_extract(op, **kw):
    arg = translate(op.arg, **kw)
    concat = ops.StringConcat(("(", op.pattern, ")"))
    pattern = translate(concat, **kw)
    if (index := getattr(op.index, "value", None)) is None:
        raise ValueError(
            "re_extract `index` expressions must be literals. "
            "Arbitrary expressions are not supported in the DataFusion backend"
        )
    string_array_get = df.udf(
        lambda arr, index=index: pc.list_element(arr, index),
        input_types=[PyArrowType.from_ibis(dt.Array(dt.string))],
        return_type=PyArrowType.from_ibis(dt.string),
        volatility="immutable",
        name="string_array_get",
    )
    return string_array_get(df.functions.regexp_match(arg, pattern))


@translate.register(ops.StringReplace)
def string_replace(op, **kw):
    arg = translate(op.arg, **kw)
    pattern = translate(op.pattern, **kw)
    replacement = translate(op.replacement, **kw)
    return df.functions.replace(arg, pattern, replacement)


@translate.register(ops.RegexReplace)
def regex_replace(op, **kw):
    arg = translate(op.arg, **kw)
    pattern = translate(op.pattern, **kw)
    replacement = translate(op.replacement, **kw)
    return df.functions.regexp_replace(arg, pattern, replacement, df.lit("g"))


@translate.register(ops.StringFind)
def string_find(op, **kw):
    if op.end is not None:
        raise NotImplementedError("`end` not yet implemented")

    arg = translate(op.arg, **kw)
    pattern = translate(op.substr, **kw)

    if (op_start := op.start) is not None:
        sub_string = ops.Substring(op.arg, op_start)
        arg = translate(sub_string, **kw)
        pos = df.functions.strpos(arg, pattern)
        start = translate(op_start, **kw)
        return df.functions.coalesce(
            df.functions.nullif(pos + start, start), df.lit(0)
        ) - df.lit(1)

    return df.functions.strpos(arg, pattern) - df.lit(1)


@translate.register(ops.RegexSearch)
def regex_search(op, **kw):
    arg = translate(op.arg, **kw)
    pattern = translate(op.pattern, **kw)

    def search(arr):
        default = pa.scalar(0, type=pa.int64())
        lengths = pc.list_value_length(arr).fill_null(default)
        return pc.greater(lengths, default)

    string_regex_search = df.udf(
        search,
        input_types=[PyArrowType.from_ibis(dt.Array(dt.string))],
        return_type=PyArrowType.from_ibis(dt.bool),
        volatility="immutable",
        name="string_regex_search",
    )

    return string_regex_search(df.functions.regexp_match(arg, pattern))


@translate.register(ops.StringContains)
def string_contains(op, **kw):
    haystack = translate(op.haystack, **kw)
    needle = translate(op.needle, **kw)

    return df.functions.strpos(haystack, needle) > df.lit(0)


@translate.register(ops.StringJoin)
def string_join(op, **kw):
    if (sep := getattr(op.sep, "value", None)) is None:
        raise ValueError(
            "join `sep` expressions must be literals. "
            "Arbitrary expressions are not supported in the DataFusion backend"
        )

    return df.functions.concat_ws(sep, *(translate(arg, **kw) for arg in op.arg))

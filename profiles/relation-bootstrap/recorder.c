#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <limits.h>
#include <stdint.h>

/* Private CPython recorder, loaded only while building the baseline index. */
#define CAPACITY 16384
#define LEFT_CONSTANT 16
#define RIGHT_CONSTANT 32
static unsigned char seen[CAPACITY], uncertain[CAPACITY], bounded[CAPACITY], unsupported[CAPACITY];
static int64_t low[CAPACITY], high[CAPACITY];
static const int rich_ops[] = {-1, -1, Py_EQ, Py_NE, Py_LT, Py_LE, Py_GT, Py_GE};
static const int alternate[] = {2, 3, 0, 1, 5, 4, 7, 6};
static const int reversed[] = {0, 1, 2, 3, 6, 7, 4, 5};

static PyObject *evaluate(int op, PyObject *a, PyObject *b) {
    if (op <= 1) return PyBool_FromLong(op == 0 ? a == b : a != b);
    return PyObject_RichCompare(a, b, rich_ops[op]);
}

static int pure_slot(PyObject *value, int op) {
    richcmpfunc slot = Py_TYPE(value)->tp_richcompare;
    if (op < 4 && (value == Py_None || slot == PyBaseObject_Type.tp_richcompare)) return 1;
    return slot == PyUnicode_Type.tp_richcompare || slot == PyBytes_Type.tp_richcompare
        || slot == PyLong_Type.tp_richcompare || slot == PyFloat_Type.tp_richcompare;
}

static void constrain(int site, int flags, int op, PyObject *a, PyObject *b) {
    if (unsupported[site]) return;
    if ((flags & LEFT_CONSTANT) != 0) {
        PyObject *tmp = a; a = b; b = tmp;
        op = reversed[op];
    }
    if (!PyLong_CheckExact(a) || !PyLong_CheckExact(b)) {
        unsupported[site] = 1;
        return;
    }
    int ao = 0, bo = 0;
    long long x = PyLong_AsLongLongAndOverflow(a, &ao), k = PyLong_AsLongLongAndOverflow(b, &bo);
    /* Avoid signed overflow at both extremes and in inverting open intervals. */
    if (ao || bo || x <= LLONG_MIN || x >= LLONG_MAX) {
        unsupported[site] = 1;
        return;
    }
    if (!bounded[site]) {
        low[site] = LLONG_MIN;
        high[site] = LLONG_MAX;
        bounded[site] = 1;
    }
    int64_t l = LLONG_MIN, h = LLONG_MAX;
    switch (op) {
        case 2: case 3:
            if (x == k) l = h = k;
            else if (x < k) l = x + 1;
            else h = x - 1;
            break;
        case 4: if (x < k) l = x + 1; else h = x; break;
        case 5: if (x <= k) l = x; else h = x - 1; break;
        case 6: if (x > k) h = x - 1; else l = x; break;
        case 7: if (x >= k) h = x; else l = x + 1; break;
    }
    if (l > low[site]) low[site] = l;
    if (h < high[site]) high[site] = h;
}

static PyObject *record(PyObject *unused, PyObject *const *args, Py_ssize_t argc) {
    if (argc != 4) return PyErr_Format(PyExc_TypeError, "expected 4 arguments");
    long site = PyLong_AsLong(args[0]), flags = PyLong_AsLong(args[1]);
    int op = (int)flags & 15;
    if (PyErr_Occurred()) return NULL;
    if (site < 0 || site >= CAPACITY || op > 7) return PyErr_Format(PyExc_ValueError, "invalid comparison site");
    PyObject *a = args[2], *b = args[3];
    if (!uncertain[site] && (!pure_slot(a, op) || !pure_slot(b, op))) uncertain[site] = 1;
    PyObject *original = evaluate(op, a, b);
    if (!original) {
        uncertain[site] = unsupported[site] = 1;
        return NULL;
    }
    if ((flags & (LEFT_CONSTANT | RIGHT_CONSTANT)) != 0) constrain(site, flags, op, a, b);
    if (!uncertain[site]) {
        if (!PyBool_Check(original)) uncertain[site] = 1;
        else {
            PyObject *other = evaluate(alternate[op], a, b);
            if (other == original) seen[site] = 1;
            else uncertain[site] = 1;
            if (!other) PyErr_Clear();
            Py_XDECREF(other);
        }
    }
    return original;
}

static int add_site(PyObject *list, int site) {
    PyObject *n = PyLong_FromLong(site);
    if (!n) return -1;
    int status = PyList_Append(list, n);
    Py_DECREF(n);
    return status;
}

static PyObject *snapshot(PyObject *unused, PyObject *args) {
    PyObject *good = PyList_New(0), *bad = PyList_New(0), *ranges = PyList_New(0), *invalid = PyList_New(0);
    if (!good || !bad || !ranges || !invalid) goto fail;
    for (int i = 0; i < CAPACITY; ++i) {
        if (seen[i] && !uncertain[i] && add_site(good, i) < 0) goto fail;
        if (uncertain[i] && add_site(bad, i) < 0) goto fail;
        if (unsupported[i] && add_site(invalid, i) < 0) goto fail;
        if (bounded[i] && !unsupported[i]) {
            PyObject *row = Py_BuildValue("(iLL)", i, (long long)low[i], (long long)high[i]);
            if (!row) goto fail;
            int status = PyList_Append(ranges, row);
            Py_DECREF(row);
            if (status < 0) goto fail;
        }
    }
    return Py_BuildValue("(NNNN)", good, bad, ranges, invalid);
fail:
    Py_XDECREF(good); Py_XDECREF(bad); Py_XDECREF(ranges); Py_XDECREF(invalid);
    return NULL;
}
static PyMethodDef methods[] = {
    {"observe", (PyCFunction)record, METH_FASTCALL, NULL},
    {"snapshot", snapshot, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_relation_native", NULL, -1, methods};
PyMODINIT_FUNC PyInit__relation_native(void) { return PyModule_Create(&module); }

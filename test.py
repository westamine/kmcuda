import os
import sys
import tempfile
import unittest

import numpy
from libKMCUDA import kmeans_cuda, supports_fp16
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_distances


class CUDA_cuda4py(object):
    @staticmethod
    def exists():
        try:
            import cuda4py
            return True
        except ImportError:
            return False

    def __init__(self):
        import cuda4py
        self.cuda4py = cuda4py
        self.objects = {}
        self.devices = self.cuda4py.Devices().devices

    def allocate(self, size, device):
        obj = self.devices[device].create_context().mem_alloc(size)
        self.objects[obj.handle] = obj
        return obj.handle

    def free(self, ptr):
        del self.objects[ptr]

    def copy_to_host(self, ptr, size, dtype):
        obj = self.objects[ptr]
        arr = numpy.zeros(size, dtype=dtype)
        obj.to_host(arr)
        return arr

    def copy_to_device(self, ptr, arr):
        obj = self.objects[ptr]
        obj.to_device(arr)

    def wrap(self, ptr, device):
        ctx = None
        for obj in self.objects.values():
            if obj.context.device is self.devices[device]:
                ctx = obj.context
                break
        self.objects[ptr] = self.cuda4py._py.MemPtr(ctx, ptr)


class CUDA_pycuda(object):
    @staticmethod
    def exists():
        try:
            import pycuda
            return True
        except ImportError:
            return False

    def __init__(self):
        import pycuda.driver as cuda
        self.cuda = cuda
        cuda.init()
        self.ctxs = [cuda.Device(i).make_context()
                     for i in range(cuda.Device.count())]
        for ctx in self.ctxs:
            ctx.pop()
        self.arrays = {}

    def allocate(self, size, device):
        ctx = self.ctxs[device]
        ctx.push()
        arr = self.cuda.mem_alloc(size)
        self.arrays[int(arr)] = ctx, arr
        ctx.pop()
        return int(arr)

    def free(self, ptr):
        ctx, _ = self.arrays[ptr]
        ctx.push()
        del self.arrays[ptr]
        ctx.pop()

    def copy_to_host(self, ptr, size, dtype):
        ctx, _ = self.arrays[ptr]
        ctx.push()
        arr = numpy.zeros(size, dtype=dtype)
        try:
            self.cuda.memcpy_dtoh(arr, ptr)
        finally:
            ctx.pop()
        return arr

    def copy_to_device(self, ptr, arr):
        ctx, _ = self.arrays[ptr]
        ctx.push()
        try:
            self.cuda.memcpy_htod(ptr, arr)
        finally:
            ctx.pop()

    def wrap(self, ptr, device):
        self.arrays[ptr] = self.ctxs[device], None


class CUDA(object):
    def __init__(self):
        if CUDA_cuda4py.exists():
            self._api = CUDA_cuda4py()
        else:
            self._api = CUDA_pycuda()

    @property
    def api(self):
        return self._api


class StdoutListener(object):
    def __init__(self):
        self._file = None
        self._stdout = ""
        self._stdout_fd_backup = None

    def __enter__(self):
        self._file = tempfile.TemporaryFile()
        self._stdout_fd_backup = os.dup(sys.stdout.fileno())
        os.dup2(self._file.fileno(), sys.stdout.fileno())

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.dup2(self._stdout_fd_backup, sys.stdout.fileno())
        self._file.seek(0)
        self._stdout = self._file.read().decode("utf-8")
        self._file.close()
        self._file = None
        os.close(self._stdout_fd_backup)
        self._stdout_fd_backup = None
        print(self._stdout)

    def __str__(self):
        return self._stdout


class KMCUDATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        numpy.random.seed(0)
        arr = numpy.empty((13000, 2), dtype=numpy.float32)
        arr[:2000] = numpy.random.rand(2000, 2) + [0, 0.5]
        arr[2000:4000] = numpy.random.rand(2000, 2) + [0, 1.5]
        arr[4000:6000] = numpy.random.rand(2000, 2) - [0, 0.5]
        arr[6000:8000] = numpy.random.rand(2000, 2) + [0.5, 0]
        arr[8000:10000] = numpy.random.rand(2000, 2) - [0.5, 0]
        arr[10000:] = numpy.random.rand(3000, 2) * 5 - [2, 2]
        cls.samples = arr
        cls.stdout = StdoutListener()

    def setUp(self):
        super(KMCUDATests, self).setUp()
        numpy.random.seed(0)

    @staticmethod
    def _reference(centroids):
        return KMeans(50, max_iter=1, init=centroids, n_init=1)

    def _validate(self, centroids, assignments, tolerance):
        ref = self._reference(centroids)
        next_asses = ref.fit_predict(self.samples)
        reasses = sum(assignments != next_asses)
        self.assertLess(reasses / len(self.samples), tolerance)

    @staticmethod
    def _get_iters_number(stdout):
        return sum(1 for l in str(stdout).split("\n") if l.startswith("iteration"))

    def test_random_lloyd(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="random", device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 7)
        self.assertEqual(sys.getrefcount(centroids), 2)
        self.assertEqual(sys.getrefcount(assignments), 2)
        self.assertEqual(sys.getrefcount(self.samples), 2)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    def test_kmeanspp_lloyd(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="kmeans++", device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 4)
        self._validate(centroids, assignments, 0.05)

    def test_kmeanspp_yinyang(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="kmeans++", device=1,
                verbosity=2, seed=3, tolerance=0.01, yinyang_t=0.1)
        self.assertEqual(self._get_iters_number(self.stdout), 15 + 3)
        self._validate(centroids, assignments, 0.01)

    def test_import_lloyd(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="random", device=1,
                verbosity=2, seed=3, tolerance=0.25, yinyang_t=0)
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init=centroids, device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        # one is 2nd stage init
        self.assertEqual(self._get_iters_number(self.stdout), 8)
        self._validate(centroids, assignments, 0.05)

    def test_random_lloyd_2gpus(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="random", device=3,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 7)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    def test_kmeanspp_lloyd_2gpus(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="k-means++", device=3,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 4)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    def test_random_lloyd_all_explicit_gpus(self):
        with self.assertRaises(ValueError):
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="random", device=0xFFFF,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)

    def test_random_lloyd_all_gpus(self):
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                self.samples, 50, init="random", device=0,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 7)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    def test_random_lloyd_host_ptr(self):
        hostptr = (self.samples.__array_interface__["data"][0],
                   -1, self.samples.shape)
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                hostptr, 50, init="random", device=0,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 7)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    def test_random_lloyd_same_device_ptr(self):
        cuda = CUDA()
        devptr = cuda.api.allocate(self.samples.size * 4, 0)
        cuda.api.copy_to_device(devptr, self.samples)
        with self.stdout:
            cdevptr, adevptr = kmeans_cuda(
                (devptr, 0, self.samples.shape), 50, init="random", device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        cuda.api.wrap(cdevptr, 0)
        cuda.api.wrap(adevptr, 0)
        try:
            self.assertEqual(self._get_iters_number(self.stdout), 7)
            self.assertIsInstance(cdevptr, int)
            self.assertIsInstance(adevptr, int)
            centroids = cuda.api.copy_to_host(
                cdevptr, 100, numpy.float32).reshape((50, 2))
            assignments = cuda.api.copy_to_host(
                adevptr, 13000, numpy.uint32)
            self._validate(centroids, assignments, 0.05)
        finally:
            cuda.api.free(devptr)
            cuda.api.free(cdevptr)
            cuda.api.free(adevptr)

    def test_random_lloyd_same_device_ptr_all_devs(self):
        cuda = CUDA()
        devptr = cuda.api.allocate(self.samples.size * 4, 0)
        cuda.api.copy_to_device(devptr, self.samples)
        with self.stdout:
            cdevptr, adevptr = kmeans_cuda(
                (devptr, 0, self.samples.shape), 50, init="random", device=0,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        cuda.api.wrap(cdevptr, 0)
        cuda.api.wrap(adevptr, 0)
        try:
            self.assertEqual(self._get_iters_number(self.stdout), 7)
            self.assertIsInstance(cdevptr, int)
            self.assertIsInstance(adevptr, int)
            centroids = cuda.api.copy_to_host(
                cdevptr, 100, numpy.float32).reshape((50, 2))
            assignments = cuda.api.copy_to_host(
                adevptr, 13000, numpy.uint32)
            self._validate(centroids, assignments, 0.05)
        finally:
            cuda.api.free(devptr)
            cuda.api.free(cdevptr)
            cuda.api.free(adevptr)

    def test_random_lloyd_different_device_ptr(self):
        cuda = CUDA()
        devptr = cuda.api.allocate(self.samples.size * 4, 0)
        cuda.api.copy_to_device(devptr, self.samples)
        with self.stdout:
            cdevptr, adevptr = kmeans_cuda(
                (devptr, 0, self.samples.shape), 50, init="random", device=2,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        cuda.api.wrap(cdevptr, 0)
        cuda.api.wrap(adevptr, 0)
        try:
            self.assertEqual(self._get_iters_number(self.stdout), 7)
            self.assertIsInstance(cdevptr, int)
            self.assertIsInstance(adevptr, int)
            centroids = cuda.api.copy_to_host(
                cdevptr, 100, numpy.float32).reshape((50, 2))
            assignments = cuda.api.copy_to_host(
                adevptr, 13000, numpy.uint32)
            self._validate(centroids, assignments, 0.05)
        finally:
            cuda.api.free(devptr)
            cuda.api.free(cdevptr)
            cuda.api.free(adevptr)

    def test_cosine_metric(self):
        arr = numpy.empty((10000, 2), dtype=numpy.float32)
        angs = numpy.random.rand(10000) * 2 * numpy.pi
        for i in range(10000):
            arr[i] = numpy.sin(angs[i]), numpy.cos(angs[i])
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                arr, 4, init="kmeans++", metric="cos", device=1, verbosity=2,
                seed=3)
        self.assertEqual(self._get_iters_number(self.stdout), 5)
        self.assertEqual(len(centroids), 4)
        for c in centroids:
            norm = numpy.linalg.norm(c)
            self.assertTrue(0.9999 < norm < 1.0001)
        dists = numpy.round(cosine_distances(centroids)).astype(int)
        self.assertTrue((dists == [
            [0, 2, 1, 1],
            [2, 0, 1, 1],
            [1, 1, 0, 2],
            [1, 1, 2, 0],
        ]).all())
        self.assertEqual(numpy.min(assignments), 0)
        self.assertEqual(numpy.max(assignments), 3)

    def test_256_features(self):
        arr = numpy.random.rand(1000, 256).astype(numpy.float32)
        arr /= numpy.linalg.norm(arr, axis=1)[:, None]
        with self.stdout:
            kmeans_cuda(
                arr, 10, init="kmeans++", metric="cos", device=0, verbosity=3,
                yinyang_t=0.1, seed=3)
        self.assertEqual(self._get_iters_number(self.stdout), 9)

    @unittest.skipUnless(supports_fp16,
                         "16-bit floats are not supported by this CUDA arch")
    def test_fp16_random_lloyd(self):
        samples = self.samples.astype(numpy.float16)
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                samples, 50, init="random", device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(centroids.dtype, numpy.float16)
        centroids = centroids.astype(numpy.float32)
        self.assertEqual(self._get_iters_number(self.stdout), 7)
        self.assertEqual(sys.getrefcount(centroids), 2)
        self.assertEqual(sys.getrefcount(assignments), 2)
        self.assertEqual(sys.getrefcount(self.samples), 2)
        self.assertEqual(centroids.shape, (50, 2))
        self.assertEqual(assignments.shape, (13000,))
        self._validate(centroids, assignments, 0.05)

    @unittest.skipUnless(supports_fp16,
                         "16-bit floats are not supported by this CUDA arch")
    def test_fp16_kmeanspp_lloyd(self):
        samples = self.samples.astype(numpy.float16)
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                samples, 50, init="kmeans++", device=1,
                verbosity=2, seed=3, tolerance=0.05, yinyang_t=0)
        self.assertEqual(self._get_iters_number(self.stdout), 5)
        centroids = centroids.astype(numpy.float32)
        self._validate(centroids, assignments, 0.05)

    @unittest.skipUnless(supports_fp16,
                         "16-bit floats are not supported by this CUDA arch")
    def test_fp16_kmeanspp_validate(self):
        centroids32, _ = kmeans_cuda(
            self.samples, 50, init="kmeans++", device=1,
            verbosity=2, seed=3, tolerance=1.0, yinyang_t=0)
        samples = self.samples.astype(numpy.float16)
        centroids16, _ = kmeans_cuda(
            samples, 50, init="kmeans++", device=1,
            verbosity=2, seed=3, tolerance=1.0, yinyang_t=0)
        delta = numpy.max(abs(centroids16[:4] - centroids32[:4]))
        self.assertLess(delta, 1.5e-4)

    @unittest.skipUnless(supports_fp16,
                         "16-bit floats are not supported by this CUDA arch")
    def test_fp16_kmeanspp_yinyang(self):
        samples = self.samples.astype(numpy.float16)
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                samples, 50, init="kmeans++", device=1,
                verbosity=2, seed=3, tolerance=0.01, yinyang_t=0.1)
        # fp16 precision increases the number of iterations
        self.assertEqual(self._get_iters_number(self.stdout), 19 + 5)
        centroids = centroids.astype(numpy.float32)
        self._validate(centroids, assignments, 0.01)

    @unittest.skipUnless(supports_fp16,
                         "16-bit floats are not supported by this CUDA arch")
    def test_fp16_cosine_metric(self):
        arr = numpy.empty((10000, 2), dtype=numpy.float16)
        angs = numpy.random.rand(10000) * 2 * numpy.pi
        for i in range(10000):
            arr[i] = numpy.sin(angs[i]), numpy.cos(angs[i])
        with self.stdout:
            centroids, assignments = kmeans_cuda(
                arr, 4, init="kmeans++", metric="cos", device=1, verbosity=2,
                seed=3)
        self.assertEqual(self._get_iters_number(self.stdout), 5)
        self.assertEqual(len(centroids), 4)
        for c in centroids:
            norm = numpy.linalg.norm(c)
            self.assertTrue(0.9995 < norm < 1.0005)
        dists = numpy.round(cosine_distances(centroids)).astype(int)
        self.assertTrue((dists == [
            [0, 2, 1, 1],
            [2, 0, 1, 1],
            [1, 1, 0, 2],
            [1, 1, 2, 0],
        ]).all())
        self.assertEqual(numpy.min(assignments), 0)
        self.assertEqual(numpy.max(assignments), 3)


if __name__ == "__main__":
    unittest.main()

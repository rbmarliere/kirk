"""
Unittests for runner module.
"""
import asyncio
import pytest
from kirk.data import Test
from kirk.data import Suite
from kirk.host import HostSUT
from kirk.scheduler import TestScheduler
from kirk.scheduler import SuiteScheduler
from kirk.scheduler import KernelTainedError
from kirk.scheduler import KernelTimeoutError
from kirk.scheduler import KernelPanicError

pytestmark = pytest.mark.asyncio


class MockHostSUT(HostSUT):
    """
    HostSUT mock.
    """

    async def get_info(self) -> dict:
        return {
            "distro": "openSUSE",
            "distro_ver": "15.3",
            "kernel": "5.10",
            "arch": "x86_64",
            "cpu": "x86_64",
            "swap": "0",
            "ram": "1M",
        }

    async def get_tainted_info(self) -> tuple:
        return 0, [""]


class MockTestScheduler(TestScheduler):
    """
    TestScheduler mock that is not checking for tainted kernel
    and it doesn't write into /dev/kmsg
    """

    async def _write_kmsg(self, test) -> None:
        pass


class MockSuiteScheduler(SuiteScheduler):
    """
    SuiteScheduler mock that traces SUT reboots.
    """

    def __init__(self, **kwargs: dict) -> None:
        super().__init__(**kwargs)
        self._scheduler = MockTestScheduler(
            sut=kwargs.get("sut", None),
            timeout=kwargs.get("exec_timeout", 3600),
            max_workers=kwargs.get("max_workers", 1)
        )
        self._rebooted = 0

    async def _restart_sut(self) -> None:
        self._logger.info("Rebooting the SUT")

        await self._scheduler.stop()
        await self._sut.stop()
        await self._sut.communicate()

        self._rebooted += 1

    @property
    def rebooted(self) -> int:
        return self._rebooted


@pytest.fixture
async def sut():
    """
    SUT object.
    """
    obj = MockHostSUT()
    obj.setup()
    await obj.communicate()
    yield obj
    await obj.stop()


class TestTestScheduler:
    """
    Tests for TestScheduler.
    """

    @pytest.fixture
    async def create_runner(self, sut):
        def _callback(
                timeout: float = 3600.0,
                max_workers: int = 1) -> TestScheduler:
            obj = MockTestScheduler(
                sut=sut,
                timeout=timeout,
                max_workers=max_workers)

            return obj

        yield _callback

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule(self, workers, create_runner):
        """
        Test the schedule method.
        """
        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["-n", "ciao"],
                parallelizable=True,
            ))

        runner = create_runner(max_workers=workers)

        await runner.schedule(tests)
        assert len(runner.results) == len(tests)

        for i in range(len(tests)):
            res = runner.results[i]
            assert res.test.name == f"test{i}"
            assert res.passed == 1
            assert res.failed == 0
            assert res.broken == 0
            assert res.skipped == 0
            assert res.warnings == 0
            assert 0 < res.exec_time < 1
            assert res.return_code == 0
            assert res.stdout == "ciao"

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_stop(self, workers, create_runner):
        """
        Test the schedule method when stop is called.
        """
        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["1"],
                parallelizable=True,
            ))

        runner = create_runner(max_workers=workers)

        async def stop():
            await asyncio.sleep(0.1)
            await runner.stop()

        await asyncio.gather(*[
            runner.schedule(tests),
            stop()
        ])

        assert len(runner.results) == 0

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_tainted(self, workers, create_runner):
        """
        Test the schedule method when kernel is tainted.
        """
        tainted = []

        async def mock_tainted():
            if tainted:
                return 1, ["proprietary module was loaded"]

            # switch to tainted status _after_ test
            tainted.append(1)
            return 0, [""]

        runner = create_runner(max_workers=workers)
        runner._get_tainted_status = mock_tainted

        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["-n", "ciao"],
                parallelizable=True,
            ))

        with pytest.raises(KernelTainedError):
            await runner.schedule(tests)

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_panic(self, workers, create_runner):
        """
        Test the schedule method on kernel panic.
        """
        tests = []
        tests.append(Test(
            name=f"test0",
            cmd="echo",
            args=["Kernel", "panic"],
            parallelizable=True,
        ))

        for i in range(1, 10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["0.2", "&&", "echo", "-n", "ciao"],
                parallelizable=True,
            ))

        runner = create_runner(max_workers=workers)

        with pytest.raises(KernelPanicError):
            await runner.schedule(tests)

        assert len(runner.results) == 1

        res = runner.results[0]
        assert res.test.name == "test0"
        assert res.passed == 0
        assert res.failed == 0
        assert res.broken == 1
        assert res.skipped == 0
        assert res.warnings == 0
        assert 0 < res.exec_time < 0.2
        assert res.return_code == -1
        assert res.stdout == "Kernel panic\n"

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_timeout(self, workers, sut, create_runner):
        """
        Test the schedule method on kernel timeout.
        """
        async def kernel_timeout(command, iobuffer=None) -> dict:
            raise asyncio.TimeoutError()

        sut.run_command = kernel_timeout
        runner = create_runner(max_workers=workers)

        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["0.1", "&&", "echo", "-n", "ciao"],
                parallelizable=True,
            ))

        with pytest.raises(KernelTimeoutError):
            await runner.schedule(tests)

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_test_timeout(
            self, workers, sut, create_runner, dummy_framework):
        """
        Test the schedule method on test timeout.
        """
        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["0.5", "&&", "echo", "-n", "ciao"],
                parallelizable=True,
            ))

        runner = create_runner(timeout=0.05, max_workers=workers)

        await runner.schedule(tests)
        assert len(runner.results) == len(tests)

        for i in range(len(tests)):
            res = runner.results[i]
            assert res.test.name == f"test{i}"
            assert res.passed == 0
            assert res.failed == 0
            assert res.broken == 1
            assert res.skipped == 0
            assert res.warnings == 0
            assert 0 < res.exec_time < 0.4
            assert res.return_code == -1
            assert res.stdout == ""


class TestSuiteScheduler:
    """
    Tests for SuiteScheduler.
    """

    @pytest.fixture
    async def create_runner(self, sut):
        def _callback(
                suite_timeout: float = 3600.0,
                exec_timeout: float = 3600.0,
                max_workers: int = 1) -> SuiteScheduler:
            obj = MockSuiteScheduler(
                sut=sut,
                suite_timeout=suite_timeout,
                exec_timeout=exec_timeout,
                max_workers=max_workers)

            return obj

        yield _callback

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule(self, workers, create_runner):
        """
        Test the schedule method.
        """
        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["-n", "ciao"],
                parallelizable=True,
            ))

        runner = create_runner(max_workers=workers)
        await runner.schedule([Suite("suite01", tests)])

        assert len(runner.results) == 1
        assert len(runner.results[0].tests_results) == 10

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_stop(self, workers, create_runner):
        """
        Test the schedule method when stop is called.
        """
        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["0.5"],
                parallelizable=True,
            ))
        runner = create_runner(max_workers=workers)

        async def stop():
            await asyncio.sleep(0.1)
            await runner.stop()

        await asyncio.gather(*[
            runner.schedule([Suite("suite01", tests)]),
            stop()
        ])

        assert len(runner.results) == 1
        assert len(runner.results[0].tests_results) == 0

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_tainted(self, workers, sut, create_runner):
        """
        Test the schedule method when kernel is tainted.
        """
        tainted = []

        async def mock_tainted():
            if tainted:
                tainted.clear()
                return 1, ["proprietary module was loaded"]

            tainted.append(1)
            return 0, []

        sut.get_tainted_info = mock_tainted
        runner = create_runner(max_workers=workers)

        tests = []
        for i in range(2):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["-n", "ciao"],
                parallelizable=True,
            ))
        await runner.schedule([Suite("suite01", tests)])

        assert runner.rebooted == 2
        assert len(runner.results) == 1
        assert len(runner.results[0].tests_results) == 2

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_panic(self, workers, create_runner):
        """
        Test the schedule method on kernel panic.
        """
        runner = create_runner(max_workers=workers)

        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["-n", "Kernel", "panic"],
                parallelizable=True,
            ))
        await runner.schedule([Suite("suite01", tests)])

        assert runner.rebooted == 10
        assert len(runner.results) == 1
        assert len(runner.results[0].tests_results) == 10

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_kernel_timeout(self, workers, sut, create_runner):
        """
        Test the schedule method on kernel timeout.
        """
        async def kernel_timeout(command, iobuffer=None) -> dict:
            raise asyncio.TimeoutError()

        sut.run_command = kernel_timeout
        runner = create_runner(max_workers=workers)

        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="echo",
                args=["ciao"],
                parallelizable=True,
            ))
        await runner.schedule([Suite("suite01", tests)])

        assert runner.rebooted > 0
        assert len(runner.results) == 1
        assert len(runner.results[0].tests_results) == len(tests)

    @pytest.mark.parametrize("workers", [1, 10])
    async def test_schedule_suite_timeout(self, workers, create_runner):
        """
        Test the schedule method on suite timeout.
        """
        runner = create_runner(suite_timeout=0.1, max_workers=workers)

        tests = []
        for i in range(10):
            tests.append(Test(
                name=f"test{i}",
                cmd="sleep",
                args=["0.5"],
                parallelizable=True,
            ))
        await runner.schedule([Suite("suite01", tests)])

        for i in range(len(tests)):
            res = runner.results[0].tests_results[i]
            assert res.test.name == f"test{i}"
            assert res.passed == 0
            assert res.failed == 0
            assert res.broken == 0
            assert res.skipped == 1
            assert res.warnings == 0
            assert 0 < res.exec_time < 0.4
            assert res.return_code == -1
            assert res.stdout == ""
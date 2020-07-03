import sys
import logging
import threading

from types import SimpleNamespace
import datetime

from mozalert import status, metrics, checkconfig
from mozalert.utils.dt import now

import re


class BaseCheck:
    """
    BaseCheck implements the thread/interval logic of a check without any
    actual execution.

    To use this class as your base class, you should implement the
    job-related methods:
        * delete_job
        * get_job_logs
        * get_job_status (SimpleNamespace)
        * set_crd_status
        * run_job
    """

    def __init__(self, **kwargs):
        """
        initialize a check
        """

        # if the process is restarted the status is re-read
        # from k8s and fed into the new check
        # this is removed from the object once its read
        self._pre_status = kwargs.get("pre_status", {})
        self.metrics_queue = kwargs.get("metrics_queue", None)

        _config = kwargs.get("config", None)
        if _config:
            self.config = _config
        else:
            self.config = checkconfig.CheckConfig(**kwargs)

        self.shutdown = False
        self._runtime = datetime.timedelta(seconds=0)
        self._thread = None
        self.escalated = False
        self._next_interval = self.config.check_interval

        self.telemetry = {}

        self._status = status.Status()

        if self._pre_status:
            self._status.parse_pre_status(**self._pre_status)
            self._next_interval = self.status.next_interval
            self._pre_status = {}

        self.start_thread()
        self.set_crd_status()

    @property
    def config(self):
        return self._config

    @property
    def status(self):
        return self._status

    @property
    def thread(self):
        return self._thread

    @property
    def shutdown(self):
        return self._shutdown

    @property
    def escalated(self):
        return self._escalated

    @property
    def next_interval(self):
        return self._next_interval

    @escalated.setter
    def escalated(self, escalated):
        self._escalated = escalated

    @shutdown.setter
    def shutdown(self, shutdown):
        self._shutdown = shutdown

    @config.setter
    def config(self, config):
        self._config = config

    def __repr__(self):
        return f"{self.config.namespace}/{self.config.name}"

    def run_job(self):
        logging.info("Executing mock run_job")

    def set_crd_status(self):
        logging.info("Executing mock set_crd_status")

    def get_job_status(self):
        logging.info("Executing mock set_status")
        return SimpleNamespace(
            active=False, succeeded=False, failed=False, start_time=None
        )

    def get_job_logs(self):
        logging.info("Executing mock get_job_logs")
        return ""

    def delete_job(self):
        logging.info("Executing mock delete_job")

    def escalate(self, recovery=False):
        self.escalated = not recovery
        logging.info("Executing mock escalation")

    @staticmethod
    def extract_telemetry_from_logs(logs):
        """
        we support some basic telemetry in log responses from checks. 
        this allows one to pass telemetry back to the controller
        without needing to implement additional clients.
        """
        _logs = ""
        _telemetry = {}
        for line in logs.split("\n"):
            pattern = re.compile(r"TELEMETRY:\s*(?P<key>\w+)\s*(?P<val>\d+)[^0-9]?")
            match = pattern.match(line)
            if not match:
                _logs += line + "\n"
                continue
            m = match.groupdict()
            key = m.get("key")
            val = m.get("val")
            _telemetry[key] = val

        return _logs, _telemetry

    def terminate(self, join=False):
        """
        stop the thread and cleanup any leftover jobs
        """
        self.shutdown = True
        logging.info(f"Terminating {self}")
        if self._thread:
            try:
                self._thread.cancel()
            except Exception as e:
                logging.info(sys.exc_info()[0])
                logging.info(e)

        if join:
            self._thread.join()

    def check(self, shutdown=lambda: False):
        """
        main thread for creating then watching a check job; this is called as
        the Timer thread target.
        """
        self.status.attempt += 1
        logging.info(f"Starting check attempt {self.status.attempt}")
        # run the job; this blocks until completion
        try:
            self.run_job(shutdown)
        except Exception as e:
            logging.info(sys.exc_info()[0])
            logging.info(e)

        self.delete_job()

        __labels__ = {
            "name": self.config.name,
            "namespace": self.config.namespace,
            "status": self.status.status.name,
            "escalated": self.escalated,
        }
        if self.status.OK and self.escalated:
            # recovery!
            self.escalate(recovery=True)
            self.status.attempt = 0
            self._next_interval = self.config.check_interval
        elif self.status.OK:
            # check passed, things are great!
            self.status.attempt = 0
            self._next_interval = self.config.check_interval
        elif self.status.attempt >= self.config.max_attempts:
            # state is not OK and we've run out of attempts. do the escalation
            self.escalate()
            self._next_interval = self.config.notification_interval
            # ^ TODO keep retrying after escalation? giveup? reset?
        else:
            # not state OK and not enough failures to escalate
            self._next_interval = self.config.retry_interval

        if self.metrics_queue:
            self.metrics_queue.put(
                metrics.MetricsQueueItem(
                    "mozalert_check_runtime", **__labels__, value=self._runtime.seconds,
                )
            )
            self.metrics_queue.put(
                metrics.MetricsQueueItem(
                    f"mozalert_check_{self.status.status.name}_count", **__labels__
                )
            )
            self.metrics_queue.put(
                metrics.MetricsQueueItem(
                    "mozalert_check_escalations",
                    **__labels__,
                    value=int(self.escalated),
                )
            )

            if self.telemetry.get("total_time"):
                self.metrics_queue.put(
                    metrics.MetricsQueueItem(
                        "mozalert_check_total_time",
                        **__labels__,
                        value=float(self.telemetry.get("total_time")),
                    )
                )

            if self.telemetry.get("latency"):
                self.metrics_queue.put(
                    metrics.MetricsQueueItem(
                        "mozalert_check_latency",
                        **__labels__,
                        value=float(self.telemetry.get("latency")),
                    )
                )

        # set the next_check for the CRD status
        self.status.next_check = now() + datetime.timedelta(seconds=self.next_interval)

        if not shutdown():
            # schedule the next run
            self.start_thread()
            # update the CRD status subresource
            self.set_crd_status()

    def start_thread(self):
        """
        starts the thread and updates the next_check time in the object.

        For this to work you must have a self.check and a self._next_interval >=0 seconds
        """
        logging.info(f"Starting {self} at interval {self.next_interval} seconds")

        self._thread = threading.Timer(
            self.next_interval, self.check, kwargs={"shutdown": lambda: self.shutdown}
        )
        self._thread.setName(f"{self}")
        self._thread.start()

        self.status.next_check = now() + datetime.timedelta(seconds=self.next_interval)

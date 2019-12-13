#
# Copyright (C) 2016 by frePPLe bvba
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from datetime import datetime
import io
from importlib import import_module
from operator import attrgetter
import os
import sys
import logging
from threading import Thread


if __name__ == "__main__":
    # Support for running in Python virtual environments
    if "VIRTUAL_ENV" in os.environ:
        activate_script = os.path.join(
            os.environ["VIRTUAL_ENV"],
            "Scripts" if os.name == "nt" else "bin",
            "activate_this.py",
        )
        exec(open(activate_script).read(), {"__file__": activate_script})

    # Initialize django
    import django

    django.setup()

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.utils.encoding import force_text

from freppledb.execute.models import Task

logger = logging.getLogger(__name__)


def clean_value(value):
    """
    A small auxilary function to handle newline characters or backslashes
    in exporting data to PostgreSQL over a COPY command.
    """
    if value is None:
        return r"\N"
    elif "\n" in value or "\\" in value:
        return value.replace("\n", "\\n").replace("\\", "\\\\")
    else:
        return value


class CopyFromGenerator(io.TextIOBase):
    """
    File-like object to handle exporting data to PostgreSQL over
    a copy command.

    Inspired on and copied from:
      https://hakibenita.com/fast-load-data-python-postgresql
    """

    def __init__(self, itr):
        self._iter = itr
        self._buff = ""

    def readable(self):
        return True

    def _read1(self, n=None):
        while not self._buff:
            try:
                self._buff = next(self._iter)
            except StopIteration:
                break
        ret = self._buff[:n]
        self._buff = self._buff[len(ret) :]
        return ret

    def read(self, n=None):
        line = []
        if n is None or n < 0:
            while True:
                m = self._read1()
                if not m:
                    break
                line.append(m)
        else:
            while n > 0:
                m = self._read1(n)
                if not m:
                    break
                n -= len(m)
                line.append(m)
        return "".join(line)


class PlanTask:
    """
    Base class for steps in the plan generation process
    """

    # Field to be set on each subclass
    description = ""
    sequence = None
    label = None

    # Fields for internal use
    task = None
    thread = "main"
    parent = None

    @classmethod
    def getWeight(cls, **kwargs):
        return 1

    @classmethod
    def run(cls, **kwargs):
        logger.warning("Warning: PlanTask doesn't implement the run method")

    @classmethod
    def display(cls, indentlevel=0, **kwargs):
        logger.info(
            "%s%s: %s (weight %s)"
            % (indentlevel * " ", cls.mainstep, cls.description, cls.weight)
        )

    @classmethod
    def _sort(self):
        pass

    @classmethod
    def _find(cls, sequence):
        if cls.sequence == sequence:
            return cls

    @classmethod
    def _remove(self, sequence):
        pass


class PlanTaskSequence(PlanTask):
    """
    Class that runs a sequence of task in sequence.
    """

    def __init__(self):
        self.steps = []

    def addTask(self, task):
        self.steps.append(task)
        task.parent = self

    def getWeight(self, **kwargs):
        total = 0
        for s in self.steps:
            s.weight = s.getWeight(**kwargs)
            if s.weight is not None and s.weight >= 0:
                total += s.weight
        return total

    def run(self, database=DEFAULT_DB_ALIAS, **kwargs):
        # Collect the list of tasks
        task_weight = self.getWeight(**kwargs)
        if not task_weight:
            task_weight = 1

        # Execute all tasks in the list
        try:
            progress = 0
            for step in self.steps:
                if step.weight is None or step.weight < 0:
                    continue

                # Update status and message
                if self.task:
                    self.task.status = "%d%%" % int(progress * 100.0 / task_weight)
                    self.task.message = step.description
                    self.task.save(using=database)

                # Run the step
                if step.thread == "main":
                    logger.info(
                        "Start step %s '%s' at %s"
                        % (
                            step.sequence,
                            step.description,
                            datetime.now().strftime("%H:%M:%S"),
                        )
                    )
                else:
                    logger.info(
                        "Start step %s %s '%s' at %s"
                        % (
                            step.thread,
                            step.step,
                            step.description,
                            datetime.now().strftime("%H:%M:%S"),
                        )
                    )
                step.run(database=database, **kwargs)
                logger.info(
                    "Finished '%s' at %s %s"
                    % (
                        step.description,
                        datetime.now().strftime("%H:%M:%S"),
                        "\n" if self.task else "",
                    )
                )
                progress += step.weight

            # Final task status
            if self.task:
                self.task.finished = datetime.now()
                self.task.status = "100%"
                self.task.message = ""
                self.task.save(using=database)
        except Exception as e:
            if self.task:
                self.task.finished = datetime.now()
                self.task.status = "Failed"
                self.task.message = str(e)
                self.task.save(using=database)
            raise

    def display(self, indentlevel=0, **kwargs):
        for i in self.steps:
            i.weight = i.getWeight(**kwargs)
            if i.weight is not None and i.weight >= 0:
                i.display(indentlevel=indentlevel, **kwargs)

    def getLabels(self, labellist):
        for t in self.steps:
            if t.label:
                lbl = (t.label[0], force_text(t.label[1]))
                if lbl not in labellist:
                    labellist.append(lbl)
        return labellist

    def _sort(self):
        self.steps = sorted(self.steps, key=attrgetter("step"))
        for i in self.steps:
            i._sort()

    def _find(self, sequence):
        for i in self.steps:
            res = i._find(sequence)
            if res:
                return res

    def _remove(self, sequence):
        for i in self.steps:
            if i.sequence == sequence:
                self.steps.remove(i)
                return


class PlanTaskParallel(PlanTask):
    """
    Class that will execute a number of tasks in parallel groups.
    """

    class _PlanTaskThread(Thread):
        def __init__(self, seq, name, **kwargs):
            super().__init__()
            self.seq = seq
            self.name = name
            self.kwargs = kwargs
            self.exception = None

        def run(self):
            try:
                self.seq.run(**self.kwargs)
            except Exception as e:
                self.exception = e

    def __init__(self):
        self.groups = {}

    def addTask(self, task):
        if task.thread not in self.groups:
            self.groups[task.thread] = PlanTaskSequence()
        self.groups[task.thread].addTask(task)

    def getWeight(self, **kwargs):
        longest = -1
        for g in self.groups.values():
            g.weight = g.getWeight(**kwargs)
            if g.weight is not None and g.weight > longest:
                longest = g.weight
        return longest

    def run(self, **kwargs):
        threads = []
        for threadname, g in self.groups.items():
            if g.weight is not None and g.weight >= 0:
                threads.append(self._PlanTaskThread(g, name=threadname, **kwargs))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            # Catch the exception from the worker thread
            if t.exception:
                logger.error("Exception caught on thread %s" % t.name)
                raise t.exception

    def display(self, indentlevel=0, **kwargs):
        for threadname, g in self.groups.items():
            g.weight = g.getWeight(**kwargs)
            if g.weight is not None and g.weight >= 0:
                logger.info(
                    "%s%s Thread %s (weight %s):"
                    % (indentlevel * " ", self.sequence, threadname, g.weight)
                )
            g.display(indentlevel=indentlevel + 2, **kwargs)

    def getLabels(self, labellist):
        for g in self.groups.values():
            g.getLabels(labellist)

    def _sort(self):
        for g in self.groups.values():
            g._sort()

    def _find(self, sequence):
        for g in self.groups.values():
            res = g._find(sequence)
            if res:
                return res

    def _remove(self, task):
        for g in self.groups.values():
            g._remove(task)


class PlanTaskRegistry:
    reg = PlanTaskSequence()

    @classmethod
    def register(cls, task):
        if not issubclass(task, PlanTask):
            logger.warning("Warning: PlanTaskRegistry only registers PlanTask objects")
        elif task.sequence is None:
            logger.warning("Warning: PlanTask doesn't have a sequence")
        else:
            # Remove a previous task at the same sequence
            cls.reg._remove(task.sequence)

            # Compute the hidden attributes
            if isinstance(task.sequence, tuple):
                task.mainstep = task.sequence[0]
                task.thread = task.sequence[1]
                task.step = task.sequence[2]
            else:
                task.mainstep = task.sequence
                task.step = task.sequence

            # Check presence of existing task at this main step
            existing = None
            for s in cls.reg.steps:
                if s.step == task.mainstep:
                    existing = s
                    break
            if isinstance(existing, PlanTaskParallel):
                # Already existing as parallel group
                s.addTask(task)
            elif not existing and task.thread == "main":
                # Simple sequential step
                cls.reg.addTask(task)
            else:
                # Create a new parallel group
                prll = PlanTaskParallel()
                prll.description = task.description[0]
                prll.sequence = task.mainstep
                prll.step = task.mainstep
                cls.reg.addTask(prll)
                prll.addTask(task)
                if existing:
                    cls.reg.steps.remove(existing)
                    prll.addTask(existing)

            if isinstance(task.description, tuple):
                task.description = task.description[1]
        return task

    @classmethod
    def getTask(cls, sequence=None):
        return cls.reg._find(sequence)

    @classmethod
    def unregister(cls, task):
        if not issubclass(task, PlanTask):
            logger.warning(
                "Warning: PlanTaskRegistry only unregisters PlanTask objects"
            )
        elif task.sequence is None:
            logger.warning("Warning: PlanTask doesn't have a sequence")
        else:
            # Removing a task from the registry
            cls.reg._remove(task.sequence)

    @classmethod
    def autodiscover(cls):
        if not cls.reg.steps:
            for app in reversed(settings.INSTALLED_APPS):
                try:
                    import_module("%s.commands" % app)
                except ImportError as e:
                    # Silently ignore if it's the commands module which isn't found
                    if str(e) not in (
                        "No module named %s.commands" % app,
                        "No module named '%s.commands'" % app,
                    ):
                        raise e
            cls.reg._sort()

    @classmethod
    def display(cls, **kwargs):
        logger.info("Planning task registry:")
        cls.reg.display(indentlevel=1, **kwargs)

    @classmethod
    def run(cls, cluster=-1, database=DEFAULT_DB_ALIAS, **kwargs):
        cls.reg.task = None
        if "FREPPLE_TASKID" in os.environ:
            try:
                cls.reg.task = (
                    Task.objects.all()
                    .using(database)
                    .get(pk=os.environ["FREPPLE_TASKID"])
                )
            except Task.DoesNotExist:
                logger.info("Task identifier not found")
        if cls.reg.task and cls.reg.task.status == "Canceling":
            cls.reg.task.status = "Cancelled"
            cls.reg.task.save(using=database)
            sys.exit(2)
        cls.reg.run(cluster=cluster, database=database, **kwargs)
        logger.info("Finished planning at %s" % datetime.now().strftime("%H:%M:%S"))

    @classmethod
    def getLabels(cls):
        labellist = []
        cls.reg.getLabels(labellist)
        return labellist


if __name__ == "__main__":
    # Select database
    try:
        database = os.environ["FREPPLE_DATABASE"] or DEFAULT_DB_ALIAS
    except:
        database = DEFAULT_DB_ALIAS

    # Use the test database if we are running the test suite
    if "FREPPLE_TEST" in os.environ:
        settings.DATABASES[database]["NAME"] = settings.DATABASES[database]["TEST"][
            "NAME"
        ]

    # Make sure the debug flag is not set!
    # When it is set, the Django database wrapper collects a list of all sql
    # statements executed and their timings. This consumes plenty of memory
    # and cpu time.
    settings.DEBUG = False

    # Send the output to a logfile
    if "FREPPLE_LOGFILE" in os.environ:
        frepple.settings.logfile = os.path.join(
            settings.FREPPLE_LOGDIR, os.environ["FREPPLE_LOGFILE"]
        )

    # Welcome message
    print("FrePPLe on %s using database '%s'" % (sys.platform, database))

    # Update the task with my processid
    if "FREPPLE_TASKID" in os.environ:
        try:
            task = (
                Task.objects.all().using(database).get(pk=os.environ["FREPPLE_TASKID"])
            )
            task.processid = os.getpid()
            task.save(update_fields=["processid"], using=database)
        except Task.DoesNotExist:
            task = None
    else:
        task = None

    # Find all planning steps and execute them
    from freppledb.common.commands import PlanTaskRegistry as register

    register.autodiscover()
    newstatus = "Done"
    try:
        register.run(database=database)
    except Exception as e:
        logger.error("Error during planning: %s" % e)
        newstatus = "Failed"
        raise
    finally:
        # Clear the processid
        if task:
            task = Task.objects.all().using(database).get(pk=task.id)
            task.processid = None
            task.status = newstatus
            task.save(update_fields=["processid", "status"], using=database)

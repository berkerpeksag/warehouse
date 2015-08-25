# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from celery import Celery, Task
from celery.signals import celeryd_init
from pyramid import scripting
from pyramid.threadlocal import get_current_request
from pyramid_tm import tm_tween_factory

from warehouse.config import Environment, configure


@celeryd_init.connect
def _configure_celery(*args, **kwargs):
    configure()


class WarehouseTask(Task):

    abstract = True

    def __call__(self, *args, **kwargs):
        registry = self.app.pyramid_config.registry
        pyramid_env = scripting.prepare(registry=registry)

        try:
            underlying = super().__call__
            if getattr(self, "pyramid", True):
                def handler(request):
                    return underlying(request, *args, **kwargs)
            else:
                def handler(request):
                    return underlying(*args, **kwargs)

            handler = tm_tween_factory(handler, pyramid_env["registry"])
            result = handler(pyramid_env["request"])
        finally:
            pyramid_env["closer"]()

        return result

    def apply_async(self, *args, **kwargs):
        # The API design of Celery makes this threadlocal pretty impossible to
        # avoid :(
        request = get_current_request()

        # If for whatever reason we were unable to get a request we'll just
        # skip this and call the original method to send this immediately.
        if request is None or not hasattr(request, "tm"):
            return super().apply_async(*args, **kwargs)

        # This will break things that expect to get an AsyncResult because
        # we're no longer going to be returning an async result from this when
        # called from within a request, response cycle. Ideally we shouldn't be
        # waiting for responses in a request/response cycle anyways though.
        request.tm.get().addAfterCommitHook(
            self._after_commit_hook,
            args=args,
            kws=kwargs,
        )

    def _after_commit_hook(self, success, *args, **kwargs):
        if success:
            super().apply_async(*args, **kwargs)


app = Celery("warehouse")
app.Task = WarehouseTask


task = app.task


def includeme(config):
    s = config.registry.settings
    app.pyramid_config = config
    app.conf.update(
        BROKER_URL=s["celery.broker_url"],
        BROKER_USE_SSL=s["warehouse.env"] == Environment.production,
        CELERY_DISABLE_RATE_LIMITS=True,
        CELERY_RESULT_BACKEND=s["celery.result_url"],
        CELERY_RESULT_SERIALIZER="json",
        CELERY_TASK_SERIALIZER="json",
        CELERY_ACCEPT_CONTENT=["json", "msgpack"],
        CELERY_MESSAGE_COMPRESSION="gzip",
        CELERY_QUEUE_HA_POLICY="all",
    )
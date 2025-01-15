#! python3.10

from wtpsplit import WtP # wtpsplit/issues/10

import logging, os, signal, time, random

import asyncio
from aioprometheus.collectors import REGISTRY
from aioprometheus.renderer import render
from aiohttp import web, ClientSession

from concurrent.futures import ThreadPoolExecutor
import threading

from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status
from opentelemetry.trace import StatusCode
from aioprometheus.collectors import Counter, Histogram, Gauge

from exorde_data import Item
from exorde_data.get_target import get_target
from process import process, TooBigError
from exorde_data.get_live_configuration import get_live_configuration, LiveConfiguration


from lab_initialization import lab_initialization
# from split_item import split_item # UNUSED TODO


from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

def setup_tracing(no_tracing=False):
    class NoOpExporter(SpanExporter):
        def export(self, __spans__):
            # Ignore the spans
            return SpanExportResult.SUCCESS

        def shutdown(self):
            # Perform any necessary shutdown operations
            pass
    logging.info("setting up tracing")
    resource = Resource(attributes={
        ResourceAttributes.SERVICE_NAME: "item_processor"
    })
    logging.info("created Tracer Provider")
    trace_provider = TracerProvider(resource=resource)

    logging.info(f"no_tracing option is {no_tracing}".format(no_tracing))
    if no_tracing:
        logging.info("NO TRACE OPTION")
        no_op_exporter = NoOpExporter()
        noop_processor = SimpleSpanProcessor(no_op_exporter)
        trace_provider.add_span_processor(noop_processor)
    else:
        logging.info("WITH TRACE OPTION")
        jaeger_exporter = JaegerExporter(
                agent_host_name="jaeger",
                agent_port=6831,
            )
        span_processor = BatchSpanProcessor(jaeger_exporter)
        trace_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(trace_provider)


app = web.Application(client_max_size=500 * 1024 * 1024)


queue_length_gauge = Gauge(
    "process_queue_length", 
    "Current number of items in the processing queue"
)
receive_counter = Counter(
    "processor_reception", 
    "Number of 'receive' calls by item processor"
)
internal_loop_count = Counter(
    "processor_cycle",
    "Amount of cycle the processor thread has ran"
)
too_big_counter = Counter(
    "processor_too_big", 
    "Number of items that are marked as too big by processor"
)
process_histogram = Histogram(
    "process_execution_duration_seconds", 
    "Duration of 'process' in seconds", 
    buckets=[0, 1, 2, 3, 4, 5, 10, 15, 20]
)
successful_process = Counter(
    "process_successful", 
    "Amount of successfully processed items"
)
process_error = Counter(
    "process_error", 
    "Amount of errored process"
)

def terminate(signal, frame):
    os._exit(0)

async def healthcheck(__request__):
    return web.Response(text="Hello world", status=200)

async def metrics(request):
    content, http_headers = render(REGISTRY, [request.headers.get("accept")])
    return web.Response(body=content, headers=http_headers)

class Busy(Exception):
    """Thread Queue is busy handeling another item"""

    def __init__(self, message="Thread queue is busy handeling another item"""):
        self.message = message
        super().__init__(self.message)


from exorde_data import CreatedAt, Content, Domain, Url, Title, ExternalId, Author, ExternalParentId

# Summary, Picture, Author, ExternalId, ExternalParentId,


async def processing_logic(app, current_item):
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("process_item") as processing_span:
        try:
            logging.info("PROCESS START")
            processed_item = process(
                current_item, app["lab_configuration"], 
                app["live_configuration"]["max_depth"]
            )
            logging.info(f"PROCESS OK - ADDING TO BATCH : {processed_item}")
            processing_span.set_status(StatusCode.OK)

            # New span for sending the processed item
            with tracer.start_as_current_span("send_processed_item") as send_span:
                target = await get_target("bpipe", "BPIPE_ADDR")
                if target:
                    async with ClientSession() as session:
                        async with session.post(
                            target, json=processed_item
                        ) as response:
                            if response.status == 200:
                                successful_process.inc({})
                                logging.info("PROCESSED ITEM SENT SUCCESSFULLY")
                                send_span.set_status(StatusCode.OK)
                            else:
                                logging.error(
                                    f"Failed to send processed item, status code: {response.status}"
                                )
                                send_span.set_status(
                                    StatusCode.ERROR, "Failed to send processed item"
                                )
                else:
                    logging.info("No batch_target configured")
                    send_span.set_status(
                        Status(StatusCode.ERROR, "No target configured")
                    )
        except asyncio.TimeoutError:
            logging.error("Processing timed out")
            processing_span.set_status(
                Status(StatusCode.ERROR, "Processing timed out")
            )
        except TooBigError as e:
            too_big_counter.inc({})
            processing_span.set_status(
                Status(StatusCode.ERROR, str(e))
            )
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            processing_span.set_status(
                Status(StatusCode.ERROR, "Unexpected error during processing")
            )
            processing_span.record_exception(e)

def thread_function(app):
    """
    Thread function that processes items. This function runs in a separate thread.
    """
    logging.info("Running thread function")
    executor = ThreadPoolExecutor()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def process_internal():
        logging.info("Running internal")
        tracer = trace.get_tracer(__name__)
        while True:
            logging.info("internal loop")
            try:
                internal_loop_count.inc({})

                """
                About usage of `process_queue.get()` inside the Thread function

                While this call is within the threading context, the actual
                waiting and retrieval from the asyncio queue is still happening
                within the asyncio event loop.

                In this particular usage, the potential concerns about asyncio
                queue's lack of thread-safety are somewhat mitigated because:
                    
                    1. Asyncio Nature: Despite being called from within a separate
                    thread, asyncio.wait_for() is still operating within the
                    asyncio context, ensuring that the asyncio queue is accessed
                    in a safe manner according to asyncio's concurrency model.

                    2. Timeout Handling: The use of asyncio.wait_for() with a 
                    timeout ensures that the thread doesn't block indefinitely
                    while waiting for an item from the queue. This helps prevent
                    potential deadlocks or long waits in the case of an empty queue.
                """
                try:
                    current_item = await asyncio.wait_for(
                        app['process_queue'].get(),
                        timeout=int(os.getenv("TIMEOUT", 1))
                    )
                except asyncio.TimeoutError:
                    current_item = None
                if current_item is not None:
                    logging.info("processing new item")
                    await asyncio.wait_for(
                        processing_logic(app, current_item),
                        timeout=int(os.getenv("TIMEOUT", 1))
                    )
            except:
                logging.exception("An error occured in processor thread")

            await asyncio.sleep(1)

    loop.run_until_complete(process_internal())


def start_processing_thread(app):
    """
    Starts the processing thread and monitors it for restart if it dies.
    """
    stop_event = threading.Event()

    def monitor_thread():
        while not stop_event.is_set():
            thread = threading.Thread(target=thread_function, args=(app, ))
            thread.start()
            thread.join()
            if not stop_event.is_set():
                logging.warning("Processing thread died. Restarting...")
    
    monitor = threading.Thread(target=monitor_thread)
    monitor.start()
    return stop_event, monitor


async def setup_thread(app):
    logging.info("creating process queue")
    app['process_queue'] = asyncio.Queue()
    logging.info("process queue created")
    stop_event, monitor_thread = start_processing_thread(app)
    app['stop_event'] = stop_event
    app['monitor_thread'] = monitor_thread
    logging.info("Thread has been setup")

# Make sure to properly handle cleanup on app shutdown
async def cleanup(app):
    app['stop_event'].set()  # Signal the thread to stop
    app['monitor_thread'].join()  # Wait for the monitor thread to finish

async def receive_item(request):
    global receive_counter
    logging.info("receiving new item")
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("receive_item") as span:
        start_time = time.time()  # Record the start time
        raw_item = await request.json()
        try:
            item: Item = Item(
                created_at=CreatedAt(raw_item['created_at']),
                title=Title(raw_item.get('title', '')),
                content=Content(raw_item['content']),
                domain=Domain(raw_item['domain']),
                url=Url(raw_item['url']),
                external_id=ExternalId(raw_item.get('external_id', '')),
                external_parent_id=ExternalParentId(raw_item.get('external_parent_id', '')),
                author=Author(raw_item.get('author', '')
            )

            process_queue = app['process_queue']

            print(process_queue)
            print(f"Item is :", item)

            await process_queue.put(item)

            queue_length_gauge.set({}, app['process_queue'].qsize())
            receive_counter.inc({})
            logging.info(item)
            span.set_status(StatusCode.OK)
            logging.info("Item added to queue")
        except Exception as e:
            logging.exception("An error occurred asserting an item structure")
            span.record_exception(e)
            span.set_status(StatusCode.ERROR, "Error processing item")

        end_time = time.time()  # Record the end time
        duration = end_time - start_time  # Calculate the duration

        span.set_attribute("processing_duration", duration)  # Record the duration as an attribute
        logging.info(f"Item handled in {duration} seconds")  # Log the duration

    return web.Response(text="received")

app.router.add_post('/', receive_item)
app.router.add_get('/', healthcheck)
app.router.add_get('/metrics', metrics)


async def configuration_init(app):
    # arguments, live_configuration
    live_configuration: LiveConfiguration = await get_live_configuration()
    lab_configuration = lab_initialization()
    app['live_configuration'] = live_configuration
    app['lab_configuration'] = lab_configuration


def start_spotter():
    logging.basicConfig(
        level=logging.DEBUG, 
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    if os.getenv("TRACE", False) == "true":
        setup_tracing()
    else:
        setup_tracing(no_tracing=True)
    port = int(os.environ.get("PORT", "8000"))
    logging.info(f"Hello World! I'm running on {port}")
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)

    logging.info(f"Starting server on {port}")
    app.on_startup.append(configuration_init)
    app.on_startup.append(setup_thread)
    try:
        asyncio.run(web._run_app(app, port=port, handle_signals=True))
    except KeyboardInterrupt:
        os._exit(0)


if __name__ == '__main__':
    start_spotter()

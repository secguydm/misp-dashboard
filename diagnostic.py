#!/usr/bin/env python3
import os
import sys
import stat
import time
import functools
import configparser
import diagnostic_util
from pprint import pprint
try:
    import redis
    import zmq
    import json
    import flask
    from halo import Halo
except ModuleNotFoundError as e:
    print('Dependency not met. Either not in a virtualenv or dependency not installed.')
    print(f'- Error: {e}')
    sys.exit(1)

'''
Steps:
- check if dependencies exists
- check if virtualenv exists
- check if configuration is update-to-date
 - check file permission
 - check if redis is running and responding
 - check if able to connect to zmq
 - check zmq_dispatcher processing queue
    - check queue status: being filled up / being filled down
- check if subscriber responding
- check if dispatcher responding
- check if server listening
- check log static endpoint
- check log dynamic endpoint
'''

configuration_file = {}


def humanize(name, isResult=False):
    words = name.split('_')
    if isResult:
        words = words[1:]
        words[0] = words[0][0].upper() + words[0][1:]
    else:
        words[0] = words[0][0].upper() + words[0][1:] + 'ing'
    return ' '.join(words)


def add_spinner(_func=None, name='dots'):
    def decorator_add_spinner(func):
        @functools.wraps(func)
        def wrapper_add_spinner(*args, **kwargs):
            human_func_name = humanize(func.__name__)
            human_func_result = humanize(func.__name__, isResult=True)
            flag_skip = False

            with Halo(text=human_func_name, spinner=name) as spinner:
                result = func(spinner, *args, **kwargs)
                if isinstance(result, tuple):
                    status, output = result
                elif isinstance(result, list):
                    status = result[0]
                    output = result[1]
                elif isinstance(result, bool):
                    status = result
                    output = None
                else:
                    status = False
                    flag_skip = True
                    spinner.fail(f'{human_func_name} -  Function return unexpected result: {str(result)}')

                if not flag_skip:
                    text = human_func_result
                    if output is not None and len(output) > 0:
                        text += f': {output}'

                    if isinstance(status, bool) and status:
                        spinner.succeed(text)
                    elif isinstance(status, bool) and not status:
                        spinner.fail(text)
                    else:
                        if status == 'info':
                            spinner.info(text)
                        else:
                            spinner.warn(text)
                return status
        return wrapper_add_spinner

    if _func is None:
        return decorator_add_spinner
    else:
        return decorator_add_spinner(_func)


@add_spinner
def check_virtual_environment(spinner):
    result = os.environ.get('VIRTUAL_ENV')
    if result is None:
        return (False, 'This diagnostic tool should be started inside a virtual environment.')
    else:
        return (True, '')


@add_spinner
def check_configuration(spinner):
    global configuration_file
    configfile = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config/config.cfg')
    cfg = configparser.ConfigParser()
    cfg.read(configfile)
    configuration_file = cfg
    cfg = {s: dict(cfg.items(s)) for s in cfg.sections()}
    configfile = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config/config.cfg.default')
    cfg_default = configparser.ConfigParser()
    cfg_default.read(configfile)
    cfg_default = {s: dict(cfg_default.items(s)) for s in cfg_default.sections()}

    # Check if all fields from config.default exists in config
    result, faulties = diagnostic_util.dict_compare(cfg_default, cfg)
    faulties = [item for sublist in faulties for item in sublist]
    if result:
        return (True, '')
    else:
        return (False, f'''Configuration incomplete.
\tUpdate your configuration file `config.cfg`.\n\t➥ Faulty fields: {", ".join(faulties)}''')


@add_spinner(name='dot')
def check_file_permission(spinner):
    max_mind_database_path = configuration_file.get('RedisMap', 'pathmaxminddb')
    st = os.stat(max_mind_database_path)
    all_read_perm = bool(st.st_mode & stat.S_IROTH)  # FIXME: permission may be changed
    if all_read_perm:
        return (True, '')
    else:
        return (False, 'Maxmin GeoDB might have incorrect read file permission')


@add_spinner
def check_redis(spinner):
    redis_server = redis.StrictRedis(
        host=configuration_file.get('RedisGlobal', 'host'),
        port=configuration_file.getint('RedisGlobal', 'port'),
        db=configuration_file.getint('RedisLog', 'db'))
    if redis_server.ping():
        return (True, '')
    else:
        return (False, '''Can\'t reach Redis server.
\t➥ Make sure it is running and adapt your configuration accordingly''')


@add_spinner
def check_zmq(spinner):
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(configuration_file.get('RedisGlobal', 'zmq_url'))
    socket.setsockopt_string(zmq.SUBSCRIBE, '')
    poller = zmq.Poller()

    poller.register(socket, zmq.POLLIN)
    socks = dict(poller.poll(timeout=15000))
    if len(socks) > 0:
        if socket in socks and socks[socket] == zmq.POLLIN:
            rcv_string = socket.recv()
            if rcv_string.startswith(b'misp_json'):
                return (True, '')
    else:
        return (False, '''Can\'t connect to the ZMQ stream.
\t➥ Make sure the MISP ZMQ is running: `/servers/serverSettings/diagnostics`
\t➥ Make sure your network infrastucture allows you to connect to the ZMQ''')


@add_spinner
def check_buffer_queue(spinner):
    redis_server = redis.StrictRedis(
            host=configuration_file.get('RedisGlobal', 'host'),
            port=configuration_file.getint('RedisGlobal', 'port'),
            db=configuration_file.getint('RedisLIST', 'db'))
    warning_threshold = 100
    elements_in_list = redis_server.llen(configuration_file.get('RedisLIST', 'listName'))
    return_status = 'warning' if elements_in_list > warning_threshold else ('info' if elements_in_list > 0 else True)
    return_text = f'Currently {elements_in_list} items in the buffer'
    return (return_status, return_text)


@add_spinner
def check_buffer_change_rate(spinner):
    redis_server = redis.StrictRedis(
        host=configuration_file.get('RedisGlobal', 'host'),
        port=configuration_file.getint('RedisGlobal', 'port'),
        db=configuration_file.getint('RedisLIST', 'db'))
    content = {'content': time.time()}
    redis_server.rpush(configuration_file.get('RedisLIST', 'listName'),
        json.dumps({'zmq_name': 'diagnostic_channel', 'content': 'diagnostic_channel ' + json.dumps(content)})
    )

    time_slept = 0
    sleep_duration = 0.001
    sleep_max = 10.0
    refresh_frequency = 1.0
    next_refresh = 0
    change_increase = 0
    change_decrease = 0
    elements_in_list_prev = 0
    elements_in_list = int(redis_server.llen(configuration_file.get('RedisLIST', 'listName')))
    elements_in_inlist_init = elements_in_list
    consecutive_no_rate_change = 0
    while True:
        elements_in_list_prev = elements_in_list
        elements_in_list = int(redis_server.llen(configuration_file.get('RedisLIST', 'listName')))
        change_increase += elements_in_list - elements_in_list_prev if elements_in_list - elements_in_list_prev > 0 else 0
        change_decrease += elements_in_list_prev - elements_in_list if elements_in_list_prev - elements_in_list > 0 else 0

        if next_refresh < time_slept:
            next_refresh = time_slept + refresh_frequency
            change_rate_text = f'↑ {change_increase}/sec\t↓ {change_decrease}/sec'
            spinner.text = f'Buffer: {elements_in_list}\t, {change_rate_text}'

            if consecutive_no_rate_change == 3:
                time_slept = sleep_max
            if elements_in_list == 0:
                consecutive_no_rate_change += 1
            else:
                consecutive_no_rate_change = 0
            change_increase = 0
            change_decrease = 0

        if time_slept >= sleep_max:
            return_flag = elements_in_inlist_init < elements_in_list or elements_in_list == 0
            return_text = f'Buffer is consumed {"faster" if return_flag else "slower" } than being populated'
            break

        time.sleep(sleep_duration)
        time_slept += sleep_duration
    elements_in_inlist_final = int(redis_server.llen(configuration_file.get('RedisLIST', 'listName')))
    return (return_flag, return_text)


@add_spinner
def check_dispatcher_status(spinner):
    redis_server = redis.StrictRedis(
        host=configuration_file.get('RedisGlobal', 'host'),
        port=configuration_file.getint('RedisGlobal', 'port'),
        db=configuration_file.getint('RedisLIST', 'db'))
    content = {'content': time.time()}
    redis_server.rpush(configuration_file.get('RedisLIST', 'listName'),
        json.dumps({'zmq_name': 'diagnostic_channel', 'content': 'diagnostic_channel ' + json.dumps(content)})
    )

    return_flag = False
    return_text = ''
    time_slept = 0
    sleep_duration = 0.2
    sleep_max = 10.0
    redis_server.delete('diagnostic_tool_response')
    while True:
        reply = redis_server.get('diagnostic_tool_response')
        elements_in_list = redis_server.llen(configuration_file.get('RedisLIST', 'listName'))
        if reply is None:
            if time_slept >= sleep_max:
                return_flag = False
                return_text = f'''zmq_dispatcher did not respond in the given time ({int(sleep_max)}sec)'''
                break
            time.sleep(sleep_duration)
            spinner.text = f'No response yet'
            time_slept += sleep_duration
        else:
            return_flag = True
            return_text = f'Took {float(reply):.2f}s to complete'
            break

    return (return_flag, return_text)


@add_spinner
def check_server_listening(spinner):
    return (False, '')


@add_spinner
def check_static_endpoint(spinner):
    return (False, '')


@add_spinner
def check_dynamic_enpoint(spinner):
    return (False, '')


def start_diagnostic():
    if not (check_virtual_environment() and check_configuration()):
        return
    check_file_permission()
    check_redis()
    # check_zmq()
    if check_buffer_queue() is not True:
        check_buffer_change_rate()
    check_dispatcher_status()
    check_server_listening()
    check_static_endpoint()
    check_dynamic_enpoint()


def main():
    start_diagnostic()


if __name__ == '__main__':
    main()

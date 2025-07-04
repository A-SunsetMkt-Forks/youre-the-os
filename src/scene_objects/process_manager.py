from math import inf
import re

from constants import ONE_SECOND
import game_monitor
from engine.game_event_type import GameEventType
from engine.scene_object import SceneObject
from engine.random import randint
from factories.process_factory import ProcessFactory
from scene_objects.checkbox import Checkbox
from scene_objects.cpu_manager import CpuManager
from scene_objects.io_queue import IoQueue
from scene_objects.process import Process
from scene_objects.views.process_manager_view import ProcessManagerView
from scene_objects.process_slot import ProcessSlot
from scene_objects.sort_button import SortButton
from window_size import WINDOW_WIDTH, WINDOW_HEIGHT

_NUM_KEYS = list(map(str, range(10))) + list(map(lambda i: f'[{str(i)}]', range(10)))

_NUM_PROCESS_SLOT_ROWS = 6
_NUM_PROCESS_SLOT_COLUMNS = 7

_MIN_SORT_COOLDOWN_MS = 100
_AUTO_SORT_CHECKBOX_ANIMATION_SPEED = 30

def _is_sorted(process_list: [Process]):
    if len(process_list) <= 1:
        return True
    for i in range(len(process_list) - 1):
        if process_list[i].sort_key > process_list[i + 1].sort_key:
            return False
    return True

class ProcessManager(SceneObject):
    def __init__(self, stage: 'Stage', stage_config: 'StageConfig'):
        self._stage = stage
        self._stage_config = stage_config

        self._process_factory = ProcessFactory(stage, stage_config)

        self._cpu_manager = None
        self._alive_process_list = None
        self._process_slots = None
        self._user_terminated_process_slots = None
        self._io_queue = None
        self._processes = None
        self._sort_processes_button = None
        self._auto_sort_checkbox = None
        self._auto_sort_checkbox_final_x_position = None

        self._next_pid = None
        self._last_new_process_check = None
        self._last_process_creation_time = None
        self._gracefully_terminated_process_count = 0
        self._user_terminated_process_count = 0
        self._sort_in_progress = False
        self._last_sort_time = 0

        self._new_process_probability_numerator = int(
            self._stage_config.new_process_probability * 100)

        if self._new_process_probability_numerator > 0:
            self._max_wait_between_new_processes = int(
                100 / self._new_process_probability_numerator * ONE_SECOND)
        else:
            self._max_wait_between_new_processes = inf

        super().__init__(ProcessManagerView(self))

    def setup(self):
        self._cpu_manager = CpuManager(self._stage_config)
        self._cpu_manager.setup()
        self.children.append(self._cpu_manager)
        self._alive_process_list = []
        self._process_slots = []
        self._user_terminated_process_slots = []
        self._io_queue = IoQueue(
            self._stage_config.io_min_waiting_time_ms, self._stage_config.io_max_waiting_time_ms
        )
        self._processes = {}

        self._next_pid = 1
        self._last_new_process_check = 0
        self._last_process_creation_time = 0
        self._user_terminated_process_count = 0

        io_queue = self._io_queue
        io_queue.view.set_xy(50, 10)
        self.children.append(io_queue)

        for row in range(_NUM_PROCESS_SLOT_ROWS):
            for column in range(_NUM_PROCESS_SLOT_COLUMNS):
                process_slot = ProcessSlot()
                x = 50 + column * process_slot.view.width + column * 5
                y = 155 + row * process_slot.view.height + row * 5
                process_slot.view.set_xy(x, y)
                self.process_slots.append(process_slot)
        self.children.extend(self.process_slots)

        for i in range(self._stage_config.max_processes_terminated_by_user):
            process_slot = ProcessSlot()
            x = 50 + i * process_slot.view.width + i * 5
            y = WINDOW_HEIGHT - process_slot.view.height - 20
            process_slot.view.set_xy(x, y)
            self._user_terminated_process_slots.append(process_slot)
        self.children.extend(self._user_terminated_process_slots)

        self._sort_processes_button = SortButton(self)
        self._sort_processes_button.view.set_xy(220, 121)
        self._sort_processes_button.visible = False
        self.children.append(self._sort_processes_button)

        self._auto_sort_checkbox = Checkbox('Auto-Sort')
        self._auto_sort_checkbox.visible = False
        self._auto_sort_checkbox.view.set_xy(
            # Initial position off-screen. An animation to move it to its
            # final position will be triggered when the checkbox becomes available.
            WINDOW_WIDTH,
            self._sort_processes_button.view.y
                + (self._sort_processes_button.view.height
                   - self._auto_sort_checkbox.view.height) // 2
        )
        self.children.append(self._auto_sort_checkbox)

        self._auto_sort_checkbox_final_x_position = (
            self._sort_processes_button.view.x + self._sort_processes_button.view.width + 10
        )

    @property
    def view_vars(self):
        return {
            'page_manager_view_width': self._stage.page_manager.view.width,
        }

    @property
    def cpu_manager(self):
        return self._cpu_manager

    @property
    def process_slots(self):
        return self._process_slots

    @property
    def io_queue(self):
        return self._io_queue

    @property
    def user_terminated_process_count(self):
        # user refers to in-game user, not to the player.
        return self._user_terminated_process_count

    @property
    def any_process_in_motion(self):
        processes_in_motion = False
        for child in self.children:
            if isinstance(child, Process):
                if child.is_in_motion:
                    processes_in_motion = True
                    break
        return processes_in_motion

    @property
    def max_processes_terminated_by_user(self):
        return self._stage_config.max_processes_terminated_by_user

    def get_process(self, pid):
        return self._processes[pid]

    def del_process(self, process):
        del self._processes[process.pid]

    def _create_process(self, process_slot_id=None):
        if len(self._alive_process_list) < self._stage_config.max_processes:
            if process_slot_id is None:
                for i, process_slot in enumerate(self.process_slots):
                    if process_slot.process is None:
                        process_slot_id = i
                        break

            pid = self._next_pid
            self._next_pid += 1

            process = self._process_factory.create_random_process(pid)

            process_slot = self.process_slots[process_slot_id]
            process_slot.process = process
            self.children.append(process)
            self._alive_process_list.append(process)

            process.view.set_xy(process_slot.view.x,
                                self.view.height + process.view.height)
            process.view.target_y = process_slot.view.y

            game_monitor.notify_process_new(pid)
            self._processes[pid] = process
            return True
        return False

    def terminate_process(self, process, by_user):
        # `by_user` refers to in-game user, not to the player.
        can_terminate = False

        if by_user:
            if (
                self._user_terminated_process_count
                < self._stage_config.max_processes_terminated_by_user
            ):
                can_terminate = True

                slot = self._user_terminated_process_slots[self._user_terminated_process_count]
                self._user_terminated_process_count += 1
                slot.process = process
                process.view.set_target_xy(slot.view.x, slot.view.y)

                self._cpu_manager.remove_process_from_cpu(process)
                for process_slot in self._process_slots:
                    if process_slot.process == process:
                        process_slot.process = None

        else:
            can_terminate = True
            self._gracefully_terminated_process_count += 1

        if can_terminate:
            self._alive_process_list.remove(process)

        return can_terminate

    def sort_idle_processes(self):
        self._sort_in_progress = True
        self._last_sort_time = self._stage.current_time
        self._continue_sorting()

    @property
    def _auto_sort_enabled(self):
        return self._auto_sort_checkbox.checked

    def _continue_sorting(self):
        """
        This method creates the visual illusion that the next recursion of the quicksort algorithm
        is performed on the idle processes. In reality, the algorithm is always performed from the
        beginning, and stops as soon as a recursion that actually changes the array has happened.
        This way, the intended in-game result is achieved while avoiding the need to keep track of
        the algorithm's state, and a correct end result is ensured even when the idle process list
        changes between recursions.
        """

        idle_processes = [slot.process for slot in self._process_slots if slot.process is not None]

        for process in idle_processes:
            if process.is_in_motion:
                return

        def simulate_next_sort_step(arr: [Process]):
            if len(arr) <= 1:
                return arr
            pivot = arr[len(arr) // 2]
            left = [process for process in arr if process.sort_key < pivot.sort_key]
            middle = [process for process in arr if process.sort_key == pivot.sort_key]
            right = [process for process in arr if process.sort_key > pivot.sort_key]
            if (left + middle + right) == arr:
                return simulate_next_sort_step(left) + middle + simulate_next_sort_step(right)
            return left + middle + right

        if _is_sorted(idle_processes):
            self._sort_in_progress = False
        else:
            idle_processes = simulate_next_sort_step(idle_processes)

        for process_slot in self._process_slots:
            process_slot.process = None
        for i, process in enumerate(idle_processes):
            process_slot = self._process_slots[i]
            process_slot.process = process
            process.view.set_target_xy(process_slot.view.x, process_slot.view.y)

    def get_current_stats(self):
        cpu_manager_stats = self._cpu_manager.get_current_stats()

        process_count_by_starvation_level = [0, 0, 0, 0, 0, 0]
        for process in self._alive_process_list:
            process_count_by_starvation_level[process.starvation_level] += 1

        return {
            'alive_process_count': len(self._alive_process_list),
            'alive_process_count_by_starvation_level': process_count_by_starvation_level,
            'active_process_count': cpu_manager_stats['active_process_count'],
            'active_process_count_by_starvation_level': cpu_manager_stats[
                'active_process_count_by_starvation_level'
            ],
            'blocked_active_process_count': cpu_manager_stats['blocked_active_process_count'],
            'io_event_count': self._io_queue.event_count,
            'gracefully_terminated_process_count': self._gracefully_terminated_process_count,
            'user_terminated_process_count': self._user_terminated_process_count,
        }

    def _handle_events(self, events):
        for event in events:
            if event.type == GameEventType.KEY_UP:
                if event.get_property('key') in _NUM_KEYS:
                    cpu_id = int(re.search(r'\d', event.get_property('key')).group())
                    if cpu_id == 0:
                        cpu_id = 10
                    if event.get_property('shift'):
                        cpu_id += 10

                    cpu = self._cpu_manager.get_cpu_by_id(cpu_id)
                    if cpu is not None and cpu.has_process:
                        cpu.process.yield_cpu()

    def _handle_process_creation(self, current_time):
        if self._next_pid <= self._stage_config.num_processes_at_startup and current_time - \
                self._last_new_process_check >= 50:
            self._last_new_process_check = current_time
            self._last_process_creation_time = current_time
            self._create_process()
        elif current_time - self._last_new_process_check >= ONE_SECOND:
            self._last_new_process_check = current_time
            if randint(1, 100) <= self._new_process_probability_numerator or current_time - \
                    self._last_process_creation_time >= self._max_wait_between_new_processes:
                self._create_process()
                self._last_process_creation_time = current_time

    def _handle_timed_powerups(self, current_time):
        if (
            self._stage.uptime_manager.uptime_ms >= self._stage_config.time_ms_to_show_sort_button
            and not self._sort_processes_button.visible
        ):
            self._sort_processes_button.visible = True
        if (
            self._stage.uptime_manager.uptime_ms
                >= self._stage_config.time_ms_to_show_auto_sort_checkbox
            and not self._auto_sort_checkbox.visible
        ):
            self._auto_sort_checkbox.visible = True
            self._auto_sort_checkbox.view.target_x = self._auto_sort_checkbox_final_x_position
        self._sort_processes_button.disabled = (
            self._sort_in_progress
            or current_time - self._last_sort_time < _MIN_SORT_COOLDOWN_MS
            or self._auto_sort_enabled
        )
        self._auto_sort_checkbox.view.move_towards_target_xy(_AUTO_SORT_CHECKBOX_ANIMATION_SPEED)

    def _handle_sorting(self):
        if self._sort_in_progress or self._auto_sort_enabled:
            self._continue_sorting()

    def _update_children(self, current_time, events):
        for scene_object in self.children:
            scene_object.update(current_time, events)
            if (
                isinstance(scene_object, Process)
                and scene_object.has_ended
                and scene_object.view.y <= -scene_object.view.height
            ):
                self.children.remove(scene_object)

    def update(self, current_time, events):
        if self._stage.game_over:
            return

        self._handle_events(events)
        self._handle_process_creation(current_time)
        self._handle_timed_powerups(current_time)
        self._handle_sorting()
        self._update_children(current_time, events)

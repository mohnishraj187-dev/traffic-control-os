import time


class SignalController:
    """Adaptive two-direction signal controller for project/demo use.

    It scores each direction using queue count plus waiting time, then switches
    through GREEN -> YELLOW -> ALL_RED -> next GREEN. This avoids unsafe instant
    flips and prevents a lighter lane from being ignored forever.
    """

    def __init__(
        self,
        min_green=20,
        max_green=70,
        yellow_seconds=3,
        all_red_seconds=2,
        queue_weight=1.0,
        wait_weight=0.18,
        switch_margin=18.0,
        low_density_threshold=35,
        high_density_threshold=65,
    ):
        self.min_green = min_green
        self.max_green = max_green
        self.yellow_seconds = yellow_seconds
        self.all_red_seconds = all_red_seconds
        self.queue_weight = queue_weight
        self.wait_weight = wait_weight
        self.switch_margin = switch_margin
        self.low_density_threshold = low_density_threshold
        self.high_density_threshold = high_density_threshold
        self.directions = ["north_south", "east_west"]
        self.active_direction = "north_south"
        self.pending_direction = None
        self.phase = "GREEN"
        self.phase_started_at = time.time()
        self.last_green_at = {name: self.phase_started_at for name in self.directions}
        self.current_green_duration = min_green

    def _traffic_pressure(self, density):
        density = max(0, min(100, int(density)))
        if density < self.low_density_threshold:
            return density * 0.25
        if density < self.high_density_threshold:
            return density
        return density * 1.35

    def _score(self, direction, count, now):
        waited = now - self.last_green_at.get(direction, now)
        return self._traffic_pressure(count) * self.queue_weight + waited * self.wait_weight

    def _green_duration(self, density):
        density = max(0, min(100, int(density)))
        if density < self.low_density_threshold:
            seconds = self.min_green
        elif density < 50:
            seconds = 30
        elif density < self.high_density_threshold:
            seconds = 40
        elif density < 85:
            seconds = 55
        else:
            seconds = self.max_green
        return min(self.max_green, max(self.min_green, seconds))

    def _state(self, ns_signal, ew_signal, now, densities, active_direction=None):
        active = active_direction or self.active_direction
        phase_elapsed = int(now - self.phase_started_at)
        remaining = max(0, int(self.current_green_duration - phase_elapsed))
        return {
            "north_south": ns_signal,
            "east_west": ew_signal,
            "phase": self.phase,
            "active_direction": active,
            "pending_direction": self.pending_direction,
            "green_duration": int(self.current_green_duration),
            "green_remaining": remaining,
            "yellow_seconds": self.yellow_seconds,
            "all_red_seconds": self.all_red_seconds,
            "scores": {
                direction: round(self._score(direction, densities.get(direction, 0), now), 2)
                for direction in self.directions
            },
        }

    def decide(self, densities):
        now = time.time()
        ns = int(densities.get("north_south", 0))
        ew = int(densities.get("east_west", 0))
        counts = {"north_south": ns, "east_west": ew}
        elapsed = now - self.phase_started_at

        if self.phase == "YELLOW":
            if elapsed >= self.yellow_seconds:
                self.phase = "ALL_RED"
                self.phase_started_at = now
                return self._state("RED", "RED", now, counts)
            return self._state("YELLOW", "YELLOW", now, counts)

        if self.phase == "ALL_RED":
            if elapsed >= self.all_red_seconds:
                self.active_direction = self.pending_direction or self.active_direction
                self.pending_direction = None
                self.phase = "GREEN"
                self.phase_started_at = now
                self.last_green_at[self.active_direction] = now
                self.current_green_duration = self._green_duration(counts.get(self.active_direction, 0))
            else:
                return self._state("RED", "RED", now, counts)

        other_direction = "east_west" if self.active_direction == "north_south" else "north_south"
        active_score = self._score(self.active_direction, counts.get(self.active_direction, 0), now)
        other_score = self._score(other_direction, counts.get(other_direction, 0), now)
        other_density = counts.get(other_direction, 0)
        active_density = counts.get(self.active_direction, 0)
        must_switch = elapsed >= self.max_green and other_density >= 15
        should_switch = (
            elapsed >= self.min_green
            and other_density >= self.low_density_threshold
            and other_score > active_score + self.switch_margin
        )
        balanced_low_traffic = (
            elapsed >= 30
            and other_density >= 15
            and active_density < self.low_density_threshold
            and other_score > active_score + 4
        )

        if must_switch or should_switch or balanced_low_traffic:
            self.pending_direction = other_direction
            self.phase = "YELLOW"
            self.phase_started_at = now
            if self.active_direction == "north_south":
                return self._state("YELLOW", "RED", now, counts)
            return self._state("RED", "YELLOW", now, counts)

        self.current_green_duration = self._green_duration(counts.get(self.active_direction, 0))
        if self.active_direction == "north_south":
            return self._state("GREEN", "RED", now, counts)
        return self._state("RED", "GREEN", now, counts)

import { useEffect, useState } from "react";

import { formatClockSeconds, formatDate, localOffset, localZoneName } from "../lib/utils";

export interface LocalClock {
  time: string;
  seconds: string;
  date: string;
  zone: string;
  offset: string;
}

function readClock(now: Date): LocalClock {
  /* formatClockSeconds yields "HH:MM:SS"; the seconds run in the small
   * accent readout next to the main HH:MM. Time comes straight from the
   * operating system — no hard-coded zone anywhere. */
  const [hours = "", minutes = "", seconds = ""] =
    formatClockSeconds(now).split(":");
  const zone = localZoneName();
  return {
    time: hours && minutes ? `${hours}:${minutes}` : "--:--",
    seconds: seconds || "--",
    date: formatDate(now),
    zone: zone ? `LOCAL · ${zone}` : "LOCAL TIME",
    offset: localOffset(now),
  };
}

export function useLocalClock(): LocalClock {
  const [clock, setClock] = useState(() => readClock(new Date()));

  useEffect(() => {
    const timer = window.setInterval(() => {
      setClock(readClock(new Date()));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  return clock;
}

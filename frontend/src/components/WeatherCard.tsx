import type { WeatherData } from "../types";
import { weatherLabel } from "../lib/utils";
import { CloudIcon, FogIcon, MoonIcon, RainIcon, RefreshIcon, SnowIcon, SunIcon } from "./ui/Icons";

interface WeatherCardProps {
  weather: WeatherData | null;
  loading: boolean;
  onRefresh: () => void;
  compact?: boolean;
}

function WeatherGlyph({ code, isDay, className }: { code?: number; isDay?: boolean; className: string }) {
  if (code === undefined) return <CloudIcon className={className} />;
  if (code === 0) return isDay === false ? <MoonIcon className={className} /> : <SunIcon className={className} />;
  if (code === 1 || code === 2 || code === 3) return <CloudIcon className={className} />;
  if (code === 45 || code === 48) return <FogIcon className={className} />;
  if ((code >= 71 && code <= 77) || (code >= 85 && code <= 86)) {
    return <SnowIcon className={className} />;
  }
  return <RainIcon className={className} />;
}

export function WeatherCard({ weather, loading, onRefresh, compact = false }: WeatherCardProps) {
  const available = Boolean(weather?.available);
  const temperature =
    typeof weather?.temperature_c === "number" ? Math.round(weather.temperature_c) : null;
  const description = weatherLabel(weather?.weather_code);
  const location = (weather?.location || "Tashkent").toUpperCase();
  const iconClass = compact ? "h-6 w-6" : "h-7 w-7";

  if (!compact) {
    return (
      <section className="aze-weather-panel" aria-label={`Weather in ${location}`}>
        <div className="flex items-center justify-between">
          <p className="aze-eyebrow">{location} // WX</p>
          <button type="button" onClick={onRefresh} disabled={loading} className="aze-ghost-icon" aria-label="Refresh weather">
            <RefreshIcon className={loading ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
          </button>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <WeatherGlyph code={weather?.weather_code} isDay={weather?.is_day} className={iconClass} />
          <div className="min-w-0">
            <p className="font-mono text-2xl font-light text-[var(--text)]">
              <span className="aze-weather-flip" key={`t-${temperature}-${available}`}>
                {available && temperature !== null ? `${temperature}°C` : "--°C"}
              </span>
            </p>
            <p className="mt-0.5 truncate text-[10px] uppercase tracking-[0.12em] text-[var(--muted)]">
              <span className="aze-weather-flip" key={`d-${description}-${available}-${loading}`}>
                {available ? description : loading ? "UPDATING" : "UNAVAILABLE"}
              </span>
              {" · "}
              {location}
            </p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <div className="aze-weather" aria-label={`Weather in ${location}`}>
      <button
        type="button"
        onClick={onRefresh}
        disabled={loading}
        className="aze-weather-main"
        title={available ? `${description} · ${weather?.location ?? "Tashkent"} · refresh` : "Refresh weather"}
      >
        <WeatherGlyph code={weather?.weather_code} isDay={weather?.is_day} className={iconClass} />
        <span className="aze-weather-temp">
          <span className="aze-weather-flip" key={`t-${temperature}-${available}`}>
            {available && temperature !== null ? `${temperature}°` : "--°"}
          </span>
        </span>
        <span className="aze-weather-copy">
          <span className="aze-weather-condition">
            <span className="aze-weather-flip" key={`d-${description}-${available}-${loading}`}>
              {available ? description : loading ? "UPDATING" : "WX UNAVAILABLE"}
            </span>
          </span>
          <span className="aze-weather-city">{location}</span>
        </span>
        {loading ? <RefreshIcon className="h-3 w-3 animate-spin text-[var(--dim)]" /> : null}
      </button>
    </div>
  );
}

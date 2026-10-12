/**
 * theme.js — Colores y tipografías de MonitorPrecios para Tailwind (CDN).
 * Se incluye en todas las pantallas, justo después del script de Tailwind,
 * para que el tema se defina en un solo lugar.
 */
tailwind.config = {
  theme: {
    extend: {
      colors: {
        fondo: "#0f172a",
        tarjeta: "#131d33",
        borde: "#24304a",
        acento: "#34d399",
      },
      fontFamily: {
        marca: ["Sora", "system-ui", "sans-serif"],
        sans: ["IBM Plex Sans", "system-ui", "sans-serif"],
      },
    },
  },
};

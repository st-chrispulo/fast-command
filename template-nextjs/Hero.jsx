export default function Hero({
  title = "Build fast. Ship faster.",
  subtitle = "Generate pages, not headaches.",
  ctaLabel = "Get Started",
  ctaHref = "/get-started",
  bgImage = "",
  align = "center",
  theme = "light",
  accent = "#35445b",
  className = ""
}) {
  const isDark = theme === "dark";
  const textAlign = {
    left: "text-left",
    center: "text-center",
    right: "text-right",
  }[align] || "text-center";

  return (
    <section
      className={`relative overflow-hidden rounded-2xl ${isDark ? "bg-neutral-900 text-white" : "bg-white"} ${className} bg-[url('/path/to/your/9bit-art-image.png')] bg-cover bg-center`}
    >
      <div className="absolute inset-0 bg-black/30" aria-hidden />
      <div className="relative mx-auto max-w-6xl px-6 py-20">
        <div className={`mx-auto max-w-3xl ${textAlign}`}>
          {title && (
            <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">
              {title}
            </h1>
          )}
          {subtitle && (
            <p className="mt-4 text-base/7 opacity-80 sm:text-lg/8">
              {subtitle}
            </p>
          )}
          {ctaLabel && (
            <a
              href={ctaHref}
              className="mt-8 inline-flex items-center justify-center rounded-2xl px-6 py-3 text-sm font-medium shadow-sm transition-transform hover:scale-[1.02] focus:outline-none focus:ring-2 focus:ring-offset-2"
              style={{ background: accent, color: "white" }}
            >
              {ctaLabel}
            </a>
          )}
        </div>
      </div>
    </section>
  );
}
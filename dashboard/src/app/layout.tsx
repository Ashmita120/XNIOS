import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "X-NioS — AI Digital Twin",
  description:
    "Operator console for the X-NioS satellite ground-station digital twin: live telemetry, network health and decision provenance.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@300;400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      {/* theme-dark is the default, exactly as on arctropy.com */}
      <body className="theme-dark">{children}</body>
    </html>
  );
}

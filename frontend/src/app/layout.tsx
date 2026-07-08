import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Financial News Dashboard",
  description: "Real-time structured financial news aggregation",
  icons: {
    icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>",
  },
  // Keep this academic/research demo out of search engines (it's a private,
  // advisor-facing project, not a public product).
  robots: { index: false, follow: false },
};

// Mobile viewport: device-width + no forced zoom, `viewport-fit=cover` so the
// dark UI extends under iOS notches/home-indicator, and a theme color that
// matches the app background so the browser chrome blends in.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#0a0e1a",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={inter.className}>{children}</body>
    </html>
  );
}

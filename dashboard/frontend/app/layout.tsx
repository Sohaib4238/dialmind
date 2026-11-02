import type { Metadata } from "next"
import { GeistMono } from "geist/font/mono"
import { GeistSans } from "geist/font/sans"

import { LoginGate } from "@/components/login-gate"
import { Providers } from "@/components/providers"
import { MobileNav, Sidebar } from "@/components/sidebar"
import { SupervisorPopup } from "@/components/supervisor-popup"
import { TestModeBanner } from "@/components/test-mode-banner"
import "./globals.css"

export const metadata: Metadata = {
  title: "DialMind — Testing",
  description: "Place test calls, watch live transcripts, edit agent instructions.",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable} dark`}>
      <body className="font-sans">
        <Providers>
          <LoginGate>
            <div className="flex min-h-dvh">
              <Sidebar />
              <main className="min-w-0 flex-1">
                <TestModeBanner />
                <MobileNav />
                <div className="w-full max-w-[1080px] px-6 py-8 md:px-8">{children}</div>
              </main>
            </div>
            <SupervisorPopup />
          </LoginGate>
        </Providers>
      </body>
    </html>
  )
}

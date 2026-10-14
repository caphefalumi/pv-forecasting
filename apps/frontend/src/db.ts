import { PrismaClient } from './generated/prisma/client.js'
import { PrismaPg } from '@prisma/adapter-pg'
import pg from 'pg'

// Strip sslmode from URL so pg respects rejectUnauthorized: false for Supabase self-signed cert chain
const rawUrl = process.env.DATABASE_URL || ''
const connectionString = rawUrl.replace(/([&?])sslmode=[^&]+(&|$)/, '$1').replace(/[?&]$/, '')

const pool = new pg.Pool({
  connectionString,
  ssl: { rejectUnauthorized: false },
})
const adapter = new PrismaPg(pool, { schema: 'app' })

declare global {
  var __prisma: PrismaClient | undefined
}

export const prisma = globalThis.__prisma || new PrismaClient({ adapter })

if (process.env.NODE_ENV !== 'production') {
  globalThis.__prisma = prisma
}




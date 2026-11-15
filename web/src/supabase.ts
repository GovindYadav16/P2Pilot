import { createClient, type SupabaseClient } from "@supabase/supabase-js";

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
export const AUTH_DISABLED = import.meta.env.VITE_AUTH_DISABLED === "true";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

export const supabase: SupabaseClient | null =
  !AUTH_DISABLED && url && anonKey ? createClient(url, anonKey) : null;

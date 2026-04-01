// POSIX compatibility shim for FidelityFX SDK on Linux.
// Provides replacements for Windows-specific functions.
#pragma once
#ifndef _WIN32

#include <cstring>
#include <cstdio>
#include <cwchar>
#include <cstdlib>
#include <string.h>
#include <wchar.h>

// MSVC _countof equivalent
#ifndef _countof
template<typename T, size_t N>
constexpr size_t _countof_impl(T (&)[N]) { return N; }
#define _countof(arr) _countof_impl(arr)
#endif

// Safe CRT replacements — 3-arg form
inline int wcscpy_s(wchar_t* dst, size_t dstSize, const wchar_t* src) {
    if (!dst || !src || dstSize == 0) return -1;
    wcsncpy(dst, src, dstSize - 1);
    dst[dstSize - 1] = L'\0';
    return 0;
}

// 2-arg template form: wcscpy_s(arr, src) — deduces array size (MSVC compat)
template<size_t N>
inline int wcscpy_s(wchar_t (&dst)[N], const wchar_t* src) {
    return wcscpy_s(dst, N, src);
}

// Safe CRT replacements — 3-arg form
inline int strcpy_s(char* dst, size_t dstSize, const char* src) {
    if (!dst || !src || dstSize == 0) return -1;
    strncpy(dst, src, dstSize - 1);
    dst[dstSize - 1] = '\0';
    return 0;
}

// 2-arg template form: strcpy_s(arr, src) — deduces array size (MSVC compat)
template<size_t N>
inline int strcpy_s(char (&dst)[N], const char* src) {
    return strcpy_s(dst, N, src);
}

#ifndef sprintf_s
#define sprintf_s(buf, size, ...) snprintf(buf, size, __VA_ARGS__)
#endif

// wchar <-> char conversion without deprecated wstring_convert
inline void ffxWideToNarrow(char* dst, size_t dstSize, const wchar_t* src) {
    if (!dst || !src || dstSize == 0) return;
    wcstombs(dst, src, dstSize - 1);
    dst[dstSize - 1] = '\0';
}

inline void ffxNarrowToWide(wchar_t* dst, size_t dstSize, const char* src) {
    if (!dst || !src || dstSize == 0) return;
    mbstowcs(dst, src, dstSize - 1);
    dst[dstSize - 1] = L'\0';
}

#endif // !_WIN32

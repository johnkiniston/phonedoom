// Headless doomgeneric platform: raw BGRA frames to a fd, 2-byte key events from stdin.
#include "doomgeneric.h"
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static int video_fd = 3;

void DG_Init(void) {
  const char *e = getenv("DOOM_VIDEO_FD");
  if (e) video_fd = atoi(e);
  fcntl(0, F_SETFL, fcntl(0, F_GETFL) | O_NONBLOCK);
}

static uint32_t last_draw = 0;
uint32_t DG_GetTicksMs(void);

#include <pthread.h>
#include <string.h>
#define FRAME_BYTES ((size_t)DOOMGENERIC_RESX * DOOMGENERIC_RESY * sizeof(pixel_t))
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static unsigned char *latest;
static int have;
static unsigned long drawn, dropped;

// Writer thread: blocks on the consumer so the game loop never does. Stale frames are dropped.
static void *writer(void *arg) {
  (void)arg;
  unsigned char *buf = malloc(FRAME_BYTES);
  for (;;) {
    pthread_mutex_lock(&mu);
    while (!have) pthread_cond_wait(&cv, &mu);
    memcpy(buf, latest, FRAME_BYTES);
    have = 0;
    pthread_mutex_unlock(&mu);
    const unsigned char *p = buf;
    size_t left = FRAME_BYTES;
    while (left) {
      ssize_t n = write(video_fd, p, left);
      if (n <= 0) exit(0);
      p += n; left -= n;
    }
  }
  return NULL;
}

void DG_DrawFrame(void) {
  uint32_t now = DG_GetTicksMs();
  if (now - last_draw < 27) return;
  last_draw = now;
  if (!latest) {
    latest = malloc(FRAME_BYTES);
    pthread_t t;
    pthread_create(&t, NULL, writer, NULL);
  }
  pthread_mutex_lock(&mu);
  if (have) dropped++;
  memcpy(latest, DG_ScreenBuffer, FRAME_BYTES);
  have = 1;
  drawn++;
  pthread_cond_signal(&cv);
  pthread_mutex_unlock(&mu);
  if (getenv("DOOM_STATS") && drawn % 140 == 0)
    fprintf(stderr, "doom frames=%lu dropped=%lu t=%ums\n", drawn, dropped, now);
}

void DG_SleepMs(uint32_t ms) { usleep(ms * 1000); }

uint32_t DG_GetTicksMs(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

int DG_GetKey(int *pressed, unsigned char *key) {
  unsigned char b[2];
  if (read(0, b, 2) != 2) return 0;
  *pressed = b[0];
  *key = b[1];
  return 1;
}

void DG_SetWindowTitle(const char *title) { (void)title; }

int main(int argc, char **argv) {
  doomgeneric_Create(argc, argv);
  for (;;) doomgeneric_Tick();
}

import hlm, time
for i in range(8):
    a = hlm.Client(); t0 = time.time(); s, d = a.login("wrong-%d" % i); print(i, s, d, "%.1fs" % (time.time() - t0))
a = hlm.Client(); t0 = time.time(); print("right pw during lockout:", a.login()[0], d if False else "", "%.1fs" % (time.time() - t0))

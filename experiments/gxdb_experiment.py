import os
import time
import findspark
import matplotlib.pyplot as plt

from gxe_utils import from_csv, from_db

from pyspark import SparkContext, SparkConf


spark_location = '/Users/Leo/spark-2.4.3-bin-hadoop2.7' # Set your own
java8_location = '/Library/Java/JavaVirtualMachines/jdk1.8.0_151.jdk/Contents/Home/jre'
os.environ['JAVA_HOME'] = java8_location
findspark.init(spark_home=spark_location)

# create gxdb from a csv file
data_file = 'data_original/ItalyPower.csv'
db_path = 'results/test_db'

mydb = from_csv(data_file, feature_num=2, num_worker=16, driver_mem=64, max_result_mem=64, _rows_to_consider=64)

# Save reloading unbuilt Genex database
mydb.save(path=db_path)
mydb.stop()
del mydb
mydb = from_db(path=db_path, num_worker=16)

start = time.time()
mydb.build(st=0.1)
print('Building took ' + str(time.time() - start) + ' sec')

# Save reloading built Genex database
mydb.save(path=db_path)
mydb.stop()
del mydb
mydb = from_db(path=db_path, num_worker=16)

# generate the query sets
q = mydb.get_random_seq_of_len(15, seed=1)

start = time.time()
# query_result = mydb.query(query=q, best_k=5, _lb_opt_repr='bsf', _lb_opt_cluster='bsf')
query_result = mydb.query(query=q, best_k=5, _radius=1, _lb_opt_repr='none')

duration = time.time() - start
# TODO memory optimization: brainstorm memory optimization, encode features (ids), length batches
# plot the query result
plt.plot(q.fetch_data(mydb.data_normalized), linewidth=5, color='red')
for qr in query_result:
    plt.plot(qr[1].fetch_data(mydb.data_normalized), color='blue')
plt.show()

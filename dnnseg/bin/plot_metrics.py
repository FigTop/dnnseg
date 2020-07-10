import re
import os
import yaml
import numpy as np
import pandas as pd
import argparse
from matplotlib import pyplot as plt
import seaborn as sns

from dnnseg.plot import MidpointNormalize
from dnnseg.util import sn

def parse_path(path, key_map):
    out = {}
    vars = os.path.basename(path).split('_')
    for k in key_map:
        out[k] = None
        for v in vars:
            if v.startswith(k):
                out[k] = v[len(k):]
        if out[k] is None:
            raise ValueError('Key %s not found in path %s.' % (k, path))

    return out



if __name__ == '__main__':
    argparser = argparse.ArgumentParser('''
    Collect evaluation metrics from multiple models and dump them into a table.
    ''')
    argparser.add_argument('table_paths', nargs='+', help='Path(s) to eval table generated by gather_metrics utility.')
    argparser.add_argument('key_map_path', help='Path to YAML file containing map from keys in parsed model directory path to human-readable names.')
    argparser.add_argument('-m', '--measures', nargs='+', help='Measures (column names of eval table) to plot.')
    argparser.add_argument('-M', '--plot_marginals', nargs='+', help='Dump bar plots showing mean performance by variable.')
    argparser.add_argument('-o', '--outdir', default='./eval_plots', help='Path to output directory.')
    args = argparser.parse_args()

    for path in args.table_paths:
        df = pd.read_csv(path, sep=' ')
        with open(args.key_map_path, 'r') as f:
            key_map = yaml.load(f)
        if 'key_order' in key_map:
            key_order = key_map.pop('key_order')
        else:
            key_order = sorted(list(key_map.keys()))
    
        paths = df.model_path.to_list()
        key_vals = [parse_path(x, key_map) for x in paths]
        df_new = pd.DataFrame(key_vals)
        for c in df_new.columns:
            if len(df_new[c].unique()) > 1:
                try:
                    df[key_map[c]] = df_new[c].astype(int)
                except ValueError:
                    try:
                        df[key_map[c]] = df_new[c].astype(float)
                    except ValueError:
                        df[key_map[c]] - df_new[c]
    
        columns = []
        index = []
        
        for i, c in enumerate(key_order):
            if i % 2 == 1:
                columns.append(key_map[c])
            else:
                index.append(key_map[c])
    
        for m in args.measures:
            pivot = df.pivot_table(index=index, columns=columns, values=m)
            pivot.dropna(axis=0, how='all', inplace=True)
            pivot.dropna(axis=1, how='all', inplace=True)
    
            ax = sns.heatmap(pivot, cmap='RdBu', center=max(0., np.nanmin(pivot.values)))
    
            outpath = args.outdir + '/'
            eval_name_parts = os.path.basename(path).split('.')
            if len(eval_name_parts) > 1:
               eval_name = '.'.join(eval_name_parts[:-1])
            else:
               eval_name = eval_name_parts[0]
            outpath += eval_name
            if not os.path.exists(outpath):
                os.makedirs(outpath)
            plt.savefig(outpath + '/%s.png' % m)
            plt.close('all')
    
            if args.plot_marginals:
                for c in [key_map[x] for x in key_order]:
                    ax = sns.barplot(x=c, y=m, data=df, n_boot=1000)
                    plt.savefig(outpath + '/%s_by_%s.png' % (m, sn(c)))
                    plt.close('all')


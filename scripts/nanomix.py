#! /usr/bin/env python

import argparse
import numpy as np
import sys
import csv
import os
import re
import pandas as pd
import pyranges as pr
import math

from scipy.stats import binom, dirichlet
from scipy.optimize import minimize, nnls, Bounds

script_dir = os.path.dirname(__file__)
ATLAS = os.path.join(script_dir, '..', 'atlases', 'meth_atlas.csv')

class ReferenceAtlas:
    def __init__(self, gr):
        self.cpg_ids = [(chrom, start, end) for chrom, start, end in\
                        zip(gr.Chromosome, gr.Start, gr.End)]
        cell_types = set(gr.columns) - {'Chromosome', 'Start', 'End', 'type'}
        self.K = len(cell_types)
        self.v = {k:list(gr[k]) for k in cell_types}
        self.A = np.array(gr.loc[:, list(cell_types)])

    def get_x(self, sigma):
        x = np.matmul(self.A, sigma)
        return x

    def get_num_cpgs(self):
        return len(self.cpg_ids)

    def get_cell_types(self):
        return list(self.v.keys())

    def get_num_cell_types(self):
        return len(self.v.keys())

class Sample:
    def __init__(self, name, x_hat, m, t):
        self.name = name
        self.x_hat = x_hat
        self.m = m
        self.t = t

def eq_constraint(x):
    return 1 - np.sum(x)

#
# Model wrappers
#
def log_likelihood_sequencing_perfect(atlas, sigma, sample, p01,p11):
    sigma_t = sigma.reshape( (atlas.K, 1) )
    x = np.clip(np.ravel(atlas.get_x(sigma_t)), 0, 1.0)
    b =  binom.logpmf(sample.m, sample.t, x)
    return np.sum(b)

def eq_constraint(x):
    return 1 - np.sum(x)
def fit_llsp(atlas, sample, p01, p11, random_inits):
    f = lambda x: -1 * log_likelihood_sequencing_perfect(atlas, x, sample, p01, p11)
    bnds = [ (0.0, 1.0) ] * atlas.K
    cons = ({'type': 'eq', 'fun': eq_constraint})
    alpha = np.array([ 1.0 / atlas.K ] * atlas.K)
    if random_inits:
        n_trials = 10
        best_ll = np.inf
        best_sol = None
        initializations = dirichlet.rvs(alpha, size=n_trials).tolist()

        for (i, init) in enumerate(initializations):
            res = minimize(f, init, method='SLSQP', options={'maxiter': 100, 'disp':False}, bounds=bnds, constraints=cons)
            ll = res.get("fun")
            if ll < best_ll:
                best_ll = ll
                best_sol = res
        return best_sol.x/np.sum(best_sol.x)
    else:
        res = minimize(f, alpha, method='SLSQP', options={'maxiter': 100, 'disp':False}, bounds=bnds, constraints=cons)
        return res.x / np.sum(res.x)

# Binomial model with sequencing errors, when p01 = 0
# this is the same as the perfect data model
def log_likelihood_sequencing_with_errors(atlas, sigma, sample, p01,p11):
    sigma_t = sigma.reshape( (atlas.K, 1) )

    # the solver we use can try values that are outside
    # the constraints we impose, we need to clip here to prevent
    # things from blowing up
    x = np.clip(np.ravel(atlas.get_x(sigma_t)), 0, 1.0)
    if p11:
        p = x * p11 + (1-x) * p01
    else:
        p = x * (1 - p01) + (1 - x) * p01
    b =  binom.logpmf(sample.m, sample.t, p)
    binomial_coef = sum([math.log(math.comb(int(t), int(m))) for m,t in zip(sample.m, sample.t)])

    return np.sum(b) - binomial_coef

def fit_llse(atlas, sample, p01, p11, random_inits):
    f = lambda x: -1 * log_likelihood_sequencing_with_errors(atlas, x, sample, p01, p11)
    bnds = [ (0.0, 1.0) ] * atlas.K
    cons = ({'type': 'eq', 'fun': eq_constraint})
    alpha = np.array([ 1.0 / atlas.K ] * atlas.K)
    if random_inits:
        n_trials = 10
        best_ll = np.inf
        best_sol = None
        initializations = dirichlet.rvs(alpha, size=n_trials).tolist()

        for (i, init) in enumerate(initializations):
            res = minimize(f, init, method='SLSQP', options={'maxiter': 100, 'disp':False}, bounds=bnds, constraints=cons)
            ll = res.get("fun")
            if ll < best_ll:
                best_ll = ll
                best_sol = res
        return best_sol.x/np.sum(best_sol.x)
    else:
        res = minimize(f, alpha, method='SLSQP', options={'maxiter': 100, 'disp':False}, bounds=bnds, constraints=cons)
        return res.x / np.sum(res.x)

def fit_nnls(atlas, sample):

    # add sum=1 constraint
    t = np.array([1.0] * atlas.K).reshape( (1, atlas.K) )
    A = np.append(atlas.A, t, axis=0)
    b = np.append(sample.x_hat, [1.0], axis=0)
    res = nnls(A, b)
    return res[0]/np.sum(res[0])

def fit_nnls_constrained(atlas, sample):
    sigma_0 = np.array([ [ 1.0 / atlas.K ] * atlas.K ])
    f = lambda x: np.linalg.norm(atlas.A.dot(x) - sample.x_hat)
    bnds = [ (0.0, 1.0) ] * atlas.K
    cons = ({'type': 'eq', 'fun': eq_constraint})
    res = minimize(f, sigma_0, method='SLSQP', options={'maxiter': 10, 'disp':False}, bounds=bnds, constraints=cons)
    return res.x

def get_sample_name(s):
    s = s.split('/')[-1]
    # label with coverage level
    # return re.search("\.\d+(\.\d+)*", s)[0][1:]
    return  s.split('.')[0]

def deconvolve(methylomes, atlas, model, p01, p11, random_inits):
    Y = []
    sample_names = []
    columns={'chromosome':'Chromosome', 'chr':'Chromosome',
                            'start':'Start',
                            'end':'End'}
    df_atlas = pd.read_csv(atlas, sep='\t').rename(columns=columns)
    df_atlas.drop_duplicates(inplace=True)
    df_atlas.dropna(inplace=True)
    gr_atlas = pr.PyRanges(df_atlas).sort()
    for methylome in methylomes:
        # read methylomes data from mbtools
        try:
            df = pd.read_csv(methylome, sep='\t').rename(columns=columns)
        except pd.errors.EmptyDataError:
            continue
        df.dropna(inplace=True)
        gr_sample = pr.PyRanges(df).sort()

        # df_sample = gr_sample.df.groupby(['Chromosome', 'Start', 'End'], as_index=False).sum()
        # gr_sample = pr.PyRanges(df_sample)
        # Init atlas and sample
        gr = gr_atlas.join(gr_sample)
        atlas = ReferenceAtlas(gr.df.loc[:, gr_atlas.columns])
        t = np.array(gr.total_calls, dtype=np.float32)
        m = np.array(gr.modified_calls, dtype=np.float32)

        # experiment with the coverage level
        # t = t*5
        # m = m*5

        xhat = m/t
        name = get_sample_name(methylome)
        sample_names.append(name)
        s = Sample(name, xhat, m, t)

        # Run
        if model == 'nnls':
            sigma = fit_nnls(atlas, s)
        elif model == 'llse':
            sigma = fit_llse(atlas, s, p01, p11, random_inits)
        elif model == 'llsp':
            sigma = fit_llsp(atlas, s, p01, p11, random_inits)
        else:
            Exception(f"no such model {model}")

        Y.append(sigma)
        print("name:\t{}".format(name))
        print("log-likelihood:\t{:.2f}".format(log_likelihood_sequencing_with_errors(atlas, sigma, s, p01, p11)))
        true_sigma = np.zeros(25)

        for i, cell_type in enumerate(atlas.get_cell_types()):
            if cell_type == 'Lung cells':
                true_sigma[i] = 0.3
            elif cell_type == 'Monocytes EPIC':
                true_sigma[i] = 0.7
        print("with true sigma ll:\t{:.6f}".format(log_likelihood_sequencing_with_errors(atlas, true_sigma, s, p01, p11)))

    return Y, sample_names, atlas

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--atlas', type=str,
            default='/.mounts/labs/simpsonlab/users/jbroadbent/code/cfdna/nanopore_cfdna/atlases/meth_atlas.csv')
    parser.add_argument('--model', default='llse', type=str, help='deconvolution model options: [nnml, llse, llsp]')
    parser.add_argument('input', nargs='+',
                        help='reference_modifications.tsv file')
    parser.add_argument('--p01', default=0.05, type=float)
    parser.add_argument('--p11', default=0.95, type=float)
    parser.add_argument('--random_inits', action='store_true')
    args = parser.parse_args()

    Y, sample_names, atlas = deconvolve(args.input, args.atlas, args.model, args.p01, args.p11, args.random_inits)

    if len(Y) < 1: Exception("No output, Deconvolution Failed")
    print("\t".join(['ct'] + sample_names))
    for i, cell_type in enumerate(atlas.get_cell_types()):
        print("\t".join([cell_type] + [str(round(y[i],4)) for y in Y]))



    # print log-likelihood




if __name__ == "__main__":
    main()
